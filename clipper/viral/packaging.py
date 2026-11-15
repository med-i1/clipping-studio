"""Job folders, final JSON package, Markdown report, and ZIP export."""

from __future__ import annotations

import json
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from clipper.viral.errors import PipelineError
from clipper.viral.models import ClipCandidate, DownloadedVideo, JobOptions, Transcript, utc_now
from clipper.viral import errors

TEXT_SUFFIXES = {".json", ".md", ".txt", ".srt", ".vtt", ".csv", ".log", ".jsonl"}


def create_job_dirs(job_root: Path) -> Dict[str, Path]:
    dirs = {
        "root": job_root,
        "original": job_root / "original",
        "transcript": job_root / "transcript",
        "clips": job_root / "clips",
        "metadata": job_root / "metadata",
        "logs": job_root / "logs",
        "temp": job_root / "temp",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _replace_with_retry(tmp, path)
    return path


def _replace_with_retry(source: Path, target: Path, attempts: int = 10, delay: float = 0.05) -> None:
    """Replace atomically, tolerating brief Windows reader locks.

    The API and GUI poll ``job.json`` while a worker is writing it. On
    Windows, ``os.replace`` fails if the target is open without delete
    sharing, so retry for a moment instead of failing the job.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def read_json(path: Path, default: Optional[Any] = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def address(root: Path, path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_result(
    job_id: str,
    source_url: str,
    created_at: str,
    options: JobOptions,
    video: DownloadedVideo,
    transcript: Transcript,
    clips: Sequence[ClipCandidate],
    job_root: Path,
    transcript_files: Dict[str, Path],
    clip_files: Dict[int, Dict[str, Optional[Path]]],
    analysis_meta: Optional[Dict[str, Any]] = None,
    original_retained: bool = True,
) -> Dict[str, Any]:
    clip_rows = []
    for clip in clips:
        files = clip_files.get(clip.rank, {})
        clip_rows.append(
            {
                "rank": clip.rank,
                "file": address(job_root, files.get("video")),
                "srt_file": address(job_root, files.get("srt")),
                "vtt_file": address(job_root, files.get("vtt")),
                "txt_file": address(job_root, files.get("txt")),
                "start_time": round(clip.start_time, 3),
                "end_time": round(clip.end_time, 3),
                "duration": round(clip.duration, 3),
                "viral_score": clip.viral_score,
                "title": clip.title,
                "hook": clip.hook,
                "reason": clip.reason,
                "summary": clip.summary,
                "category": clip.category,
                "platforms": list(clip.platforms),
                "keywords": list(clip.keywords),
                "confidence": clip.confidence,
                "adjustments": list(clip.adjustments),
            }
        )
    return {
        "schema_version": 1,
        "job_id": job_id,
        "source_url": source_url,
        "created_at": created_at,
        "status": "completed",
        "options": options.to_dict(),
        "video": {
            "title": video.title,
            "duration": round(video.duration, 3),
            "original_file": address(job_root, video.path) if original_retained else None,
            "original_retained": original_retained,
            "quality": video.quality,
            "filesize": video.filesize,
            "thumbnail": address(job_root, video.thumbnail),
            "uploader": video.uploader,
            "from_cache": video.from_cache,
        },
        "transcript": {
            "language": transcript.language,
            "provider": transcript.provider,
            "segments": len(transcript.segments),
            "raw_file": address(job_root, transcript_files.get("raw")),
            "clean_file": address(job_root, transcript_files.get("clean")),
            "srt_file": address(job_root, transcript_files.get("srt")),
            "vtt_file": address(job_root, transcript_files.get("vtt")),
            "txt_file": address(job_root, transcript_files.get("txt")),
        },
        "analysis": analysis_meta or {},
        "clips": clip_rows,
    }


def _escape_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def write_report(result: Dict[str, Any], path: Path) -> Path:
    video = result.get("video", {})
    clips = result.get("clips", [])
    lines = [
        "# Viral Clip Report",
        "",
        f"**Video:** {video.get('title', 'Unknown')}",
        f"**Source:** {result.get('source_url', '')}",
        f"**Duration:** {format_duration(video.get('duration', 0))}",
        f"**Clips:** {len(clips)}",
        f"**Job:** `{result.get('job_id', '')}`",
        f"**Created:** {result.get('created_at', '')}",
        f"**Quality:** {video.get('quality', '')}",
        "",
        "## Clips",
        "",
        "| # | Title | Start | End | Length | Score | Category | Platforms | File |",
        "|---|-------|-------|-----|--------|-------|----------|-----------|------|",
    ]
    for clip in clips:
        lines.append(
            "| {rank} | {title} | {start} | {end} | {length}s | {score} | {category} | {platforms} | {file} |".format(
                rank=clip.get("rank", ""),
                title=_escape_cell(clip.get("title")),
                start=format_duration(clip.get("start_time", 0)),
                end=format_duration(clip.get("end_time", 0)),
                length=round(float(clip.get("duration", 0)), 1),
                score=clip.get("viral_score", ""),
                category=_escape_cell(clip.get("category")),
                platforms=_escape_cell(", ".join(clip.get("platforms", []))),
                file=_escape_cell(clip.get("file")),
            )
        )
    lines.extend(["", "## Why each clip was selected", ""])
    for clip in clips:
        lines.extend(
            [
                f"### {clip.get('rank', '')}. {clip.get('title', '')}",
                "",
                f"> {clip.get('hook', '')}",
                "",
                f"- **Window:** {clip.get('start_time', 0):.1f}s → {clip.get('end_time', 0):.1f}s "
                f"({clip.get('duration', 0):.1f}s)",
                f"- **Viral score:** {clip.get('viral_score', 0)}/100",
                f"- **Category:** {clip.get('category', '')}",
                f"- **Reason:** {clip.get('reason', '')}",
                f"- **Summary:** {clip.get('summary', '')}",
                f"- **Keywords:** {', '.join(clip.get('keywords', []))}",
                f"- **File:** `{clip.get('file', '')}`",
                "",
            ]
        )
        if clip.get("adjustments"):
            lines.append(f"- **Timing repairs:** {', '.join(clip['adjustments'])}")
            lines.append("")
    if not clips:
        lines.append("_No clips were produced._")
    lines.extend(
        [
            "",
            "---",
            "Generated by Clipping Studio's viral pipeline.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_zip(
    job_root: Path,
    result_path: Path,
    report_path: Path,
    output_path: Path,
    include_original: bool = True,
) -> Path:
    """Package the useful artifacts, excluding temp folders and nested zips."""
    job_root = job_root.resolve()
    output_path = output_path.resolve()
    include_roots = ["clips", "transcript", "metadata", "logs"]
    if include_original:
        include_roots.append("original")
    files: List[Path] = [result_path.resolve(), report_path.resolve()]
    for root_name in include_roots:
        root = job_root / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append(path.resolve())
    seen = set()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path in files:
                if not path.exists() or path in seen or path == output_path:
                    continue
                seen.add(path)
                try:
                    arcname = path.relative_to(job_root).as_posix()
                except ValueError:
                    continue
                if arcname.startswith("..") or Path(arcname).is_absolute():
                    continue
                compression = zipfile.ZIP_STORED if path.suffix.lower() in {".mp4", ".m4v", ".mov", ".mkv", ".webm"} else zipfile.ZIP_DEFLATED
                archive.write(path, arcname, compress_type=compression)
    except OSError as exc:
        raise PipelineError(
            errors.PACKAGING_FAILED,
            "The final ZIP package could not be created.",
            technical_details=str(exc),
            retryable=True,
        ) from exc
    return output_path


def write_clip_metadata(
    clip: ClipCandidate,
    metadata_dir: Path,
    files: Dict[str, Optional[Path]],
) -> Path:
    data = clip.to_dict()
    data["files"] = {key: (str(value) if value else None) for key, value in files.items()}
    return write_json(metadata_dir / f"clip_{clip.rank:03d}.json", data)
