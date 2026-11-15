"""Command-line interface for the viral clip pipeline."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from clipper.viral import __version__
from clipper.viral.config import ViralConfig, load_config
from clipper.viral.errors import PipelineError
from clipper.viral.jobs import JobManager, spawn_detached_worker
from clipper.viral.models import JobRecord, SUPPORTED_PLATFORMS


def _options_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    platforms = args.platforms or list(SUPPORTED_PLATFORMS)
    return {
        "language": args.language,
        "number_of_clips": args.clips,
        "min_clip_duration": args.min_duration,
        "max_clip_duration": args.max_duration,
        "platforms": platforms,
        "download_original": not args.discard_original,
        "generate_subtitles": not args.no_subtitles,
        "download_format": args.format,
    }


def _print_record(record: JobRecord, as_json: bool = False) -> None:
    data = record.to_dict()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"job_id: {record.job_id}")
    print(f"status: {record.status}")
    print(f"progress: {record.progress}%")
    print(f"step: {record.current_step}")
    print(f"output: {record.output_path}")
    if record.error:
        print(f"error: {record.error.get('code')}: {record.error.get('message')}")
    if record.result_path:
        print(f"result: {record.result_path}")
    if record.report_path:
        print(f"report: {record.report_path}")
    if record.package_path:
        print(f"package: {record.package_path}")


def _wait_with_progress(manager: JobManager, job_id: str, timeout: Optional[float] = None) -> JobRecord:
    deadline = time.monotonic() + timeout if timeout else None
    last = (-1.0, "")
    while True:
        record = manager.get_job(job_id)
        marker = (record.progress, record.current_step)
        if marker != last:
            print(f"[{record.progress:5.1f}%] {record.current_step}", file=sys.stderr)
            last = marker
        if record.status in ("completed", "failed"):
            return record
        if deadline is not None and time.monotonic() >= deadline:
            raise PipelineError("TIMEOUT", "Timed out waiting for the job to finish.", retryable=True)
        time.sleep(0.7)


def cmd_run(args: argparse.Namespace, config: ViralConfig) -> int:
    manager = JobManager(config)
    try:
        record = manager.create_job(
            args.url, _options_from_args(args), submit=not args.no_wait
        )
        print(f"job_id: {record.job_id}")
        print("status: queued")
        if args.no_wait:
            worker = spawn_detached_worker(config, record.job_id)
            print(f"worker: started (pid {worker.pid})")
            return 0
        record = _wait_with_progress(manager, record.job_id, args.timeout)
        _print_record(record, as_json=args.json)
        return 0 if record.status == "completed" else 1
    finally:
        manager.shutdown(wait=False)


def cmd_status(args: argparse.Namespace, config: ViralConfig) -> int:
    manager = JobManager(config)
    try:
        record = manager.get_job(args.job_id)
        _print_record(record, as_json=args.json)
        return 0
    finally:
        manager.shutdown(wait=False)


def cmd_list(args: argparse.Namespace, config: ViralConfig) -> int:
    manager = JobManager(config)
    try:
        records = manager.list_jobs(limit=args.limit)
        if args.json:
            print(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))
            return 0
        if not records:
            print("No viral jobs found.")
            return 0
        print(f"{'JOB ID':34} {'STATUS':16} {'PROGRESS':>8}  CREATED")
        for record in records:
            print(f"{record.job_id:34} {record.status:16} {record.progress:7.1f}%  {record.created_at}")
        return 0
    finally:
        manager.shutdown(wait=False)


def cmd_retry(args: argparse.Namespace, config: ViralConfig) -> int:
    manager = JobManager(config)
    try:
        record = manager.retry_job(args.job_id, submit=args.wait)
        print(f"job_id: {record.job_id}")
        print("status: queued")
        if args.wait:
            record = _wait_with_progress(manager, record.job_id, args.timeout)
            _print_record(record, as_json=args.json)
            return 0 if record.status == "completed" else 1
        worker = spawn_detached_worker(config, record.job_id)
        print(f"worker: started (pid {worker.pid})")
        return 0
    finally:
        manager.shutdown(wait=False)


def cmd_serve(args: argparse.Namespace, config: ViralConfig) -> int:
    from clipper.viral.server import run_server

    run_server(config, host=args.host or config.api_host, port=args.port or config.api_port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="viral-clips",
        description="Turn a long video URL into ranked short clips.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Create and process a viral clipping job.")
    run.add_argument("--url", required=True, help="Public http(s) video URL.")
    run.add_argument("--clips", type=int, default=5, help="Number of clips (default: 5).")
    run.add_argument("--min-duration", type=float, default=20.0, help="Minimum clip seconds.")
    run.add_argument("--max-duration", type=float, default=90.0, help="Maximum clip seconds.")
    run.add_argument("--language", default="auto", help="auto, ar, en, ...")
    run.add_argument("--platforms", nargs="+", choices=list(SUPPORTED_PLATFORMS), default=None)
    run.add_argument("--no-subtitles", action="store_true", help="Skip per-clip SRT/VTT/caption files.")
    run.add_argument("--discard-original", action="store_true", help="Do not keep the original video after clipping.")
    run.add_argument("--format", default=None, help="yt-dlp format selector override.")
    run.add_argument("--no-wait", action="store_true", help="Return immediately and run the job in a detached worker.")
    run.add_argument("--timeout", type=float, default=None, help="Maximum seconds to wait.")
    run.add_argument("--json", action="store_true", help="Print the final job record as JSON.")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="Show one job.")
    status.add_argument("job_id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status, timeout=None)

    listing = sub.add_parser("list", help="List jobs.")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    retry = sub.add_parser("retry", help="Retry a failed job.")
    retry.add_argument("job_id")
    retry.add_argument("--wait", action="store_true")
    retry.add_argument("--timeout", type=float, default=None)
    retry.add_argument("--json", action="store_true")
    retry.set_defaults(func=cmd_retry)

    serve = sub.add_parser("serve", help="Run the local results web UI / JSON API.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    try:
        return args.func(args, config)
    except PipelineError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        if exc.technical_details:
            print(f"details: {exc.technical_details}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
