"""Detached worker process for one queued viral clipping job."""

from __future__ import annotations

import argparse
from typing import List, Optional

from clipper.viral.cli import _print_record
from clipper.viral.config import load_config
from clipper.viral.errors import PipelineError
from clipper.viral.jobs import JobManager


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="viral-clips-worker")
    parser.add_argument("job_id")
    args = parser.parse_args(argv)

    config = load_config()
    manager = JobManager(config)
    try:
        record = manager.execute_job(args.job_id)
        _print_record(record)
        return 0 if record.status == "completed" else 1
    except PipelineError as exc:
        print(f"ERROR {exc.code}: {exc.message}")
        return 2
    finally:
        manager.shutdown(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
