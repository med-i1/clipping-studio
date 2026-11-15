"""Thin ffmpeg/ffprobe helpers used by transcription and extraction."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from clipper.viral import errors
from clipper.viral.errors import PipelineError


def tool_available(binary: str) -> bool:
    try:
        result = subprocess.run(
            [binary, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def require_tools(ffmpeg_bin: str, ffprobe_bin: str) -> None:
    missing = [name for name, binary in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)) if not tool_available(binary)]
    if missing:
        raise PipelineError(
            errors.FFMPEG_NOT_FOUND,
            "FFmpeg was not found. Install FFmpeg and add it to PATH.",
            technical_details=f"Missing: {', '.join(missing)}",
        )


def run_command(
    cmd: List[str],
    timeout: Optional[int] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise PipelineError(
            errors.FFMPEG_NOT_FOUND,
            "A required media tool was not found.",
            technical_details=f"{cmd[0]}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(
            errors.TIMEOUT,
            "A media command took too long and was stopped.",
            technical_details=f"{cmd[0]} timed out after {timeout}s",
            retryable=True,
        ) from exc
    if check and result.returncode != 0:
        raise PipelineError(
            errors.CLIP_EXTRACTION_FAILED,
            "FFmpeg could not process this video.",
            technical_details=(result.stderr or "")[-2000:],
            retryable=True,
        )
    return result


def probe_media(path: Path, ffprobe_bin: str = "ffprobe") -> Dict[str, Any]:
    result = run_command(
        [
            ffprobe_bin,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=60,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise PipelineError(
            errors.CLIP_EXTRACTION_FAILED,
            "The downloaded video could not be inspected.",
            technical_details=result.stdout[-1000:],
        ) from exc
    return data if isinstance(data, dict) else {}


def probe_duration(path: Path, ffprobe_bin: str = "ffprobe") -> Optional[float]:
    try:
        data = probe_media(path, ffprobe_bin)
        duration = data.get("format", {}).get("duration")
        if duration is None:
            for stream in data.get("streams", []):
                if stream.get("duration") is not None:
                    duration = stream["duration"]
                    break
        return float(duration) if duration is not None else None
    except (PipelineError, TypeError, ValueError):
        return None


def stream_types(path: Path, ffprobe_bin: str = "ffprobe") -> List[str]:
    try:
        data = probe_media(path, ffprobe_bin)
    except PipelineError:
        return []
    return [str(s.get("codec_type")) for s in data.get("streams", []) if s.get("codec_type")]


def has_audio_stream(path: Path, ffprobe_bin: str = "ffprobe") -> bool:
    return "audio" in stream_types(path, ffprobe_bin)


def has_video_stream(path: Path, ffprobe_bin: str = "ffprobe") -> bool:
    return "video" in stream_types(path, ffprobe_bin)


def stream_codecs(path: Path, ffprobe_bin: str = "ffprobe") -> List[tuple]:
    """Return ``(codec_type, codec_name)`` pairs for every stream."""
    try:
        data = probe_media(path, ffprobe_bin)
    except PipelineError:
        return []
    return [
        (str(stream.get("codec_type") or ""), str(stream.get("codec_name") or ""))
        for stream in data.get("streams", [])
    ]


def extract_audio(
    video_path: Path,
    output_path: Path,
    ffmpeg_bin: str = "ffmpeg",
    timeout: int = 1800,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libmp3lame", "-b:a", "64k",
            "-y", str(output_path),
        ],
        timeout=timeout,
    )
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise PipelineError(
            errors.AUDIO_EXTRACTION_FAILED,
            "The audio track could not be extracted from this video.",
            retryable=True,
        )
    return output_path


def cut_audio_chunk(
    audio_path: Path,
    output_path: Path,
    start: float,
    duration: float,
    ffmpeg_bin: str = "ffmpeg",
    timeout: int = 600,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(audio_path),
            "-t", f"{duration:.3f}",
            "-ac", "1", "-ar", "16000",
            "-c:a", "libmp3lame", "-b:a", "64k",
            "-y", str(output_path),
        ],
        timeout=timeout,
    )
    return output_path
