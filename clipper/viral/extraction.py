"""FFmpeg clip extraction with stream-copy first and a safe re-encode fallback."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional, Protocol

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.media import (
    has_video_stream,
    probe_duration,
    require_tools,
    run_command,
    stream_codecs,
)
from clipper.viral.models import ClipCandidate, DownloadedVideo


def clip_filename(index: int, clip: ClipCandidate) -> str:
    """Stable, human-readable, path-safe clip name."""
    score = max(0, min(100, int(round(clip.viral_score))))
    rank = max(1, int(clip.rank))
    return f"clip_{index:03d}_rank_{rank}_viral_score_{score}.mp4"


class ClipExtractor(Protocol):
    def extract(self, video: DownloadedVideo, clip: ClipCandidate, output_path: Path) -> Path:
        ...


class FFmpegClipExtractor:
    def __init__(self, config: ViralConfig, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    def extract(self, video: DownloadedVideo, clip: ClipCandidate, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self._existing_output_is_valid(output_path, clip.duration):
            if self.logger:
                self.logger.event("clip_reused", path=str(output_path))
            return output_path

        require_tools(self.config.ffmpeg_bin, self.config.ffprobe_bin)
        mode = (self.config.clip_copy_mode or "auto").lower()
        if mode not in ("auto", "copy", "reencode"):
            mode = "auto"
        if output_path.exists():
            output_path.unlink(missing_ok=True)

        copy_allowed = mode == "copy" or (
            mode == "auto" and self._copy_is_platform_safe(video.path)
        )
        if mode == "auto" and not copy_allowed and self.logger:
            self.logger.event(
                "clip_copy_skipped",
                reason="incompatible_codecs",
                path=str(video.path),
            )

        if copy_allowed:
            self._extract_copy(video.path, clip, output_path)
            if self._existing_output_is_valid(output_path, clip.duration):
                if self.logger:
                    self.logger.event("clip_extracted", mode="copy", path=str(output_path))
                return output_path
            output_path.unlink(missing_ok=True)
            if mode == "copy":
                raise PipelineError(
                    errors.CLIP_EXTRACTION_FAILED,
                    "The clip could not be copied without re-encoding.",
                    technical_details=f"Stream copy produced an invalid clip for {output_path.name}.",
                    retryable=False,
                )

        self._extract_reencode(video.path, clip, output_path)
        if not self._existing_output_is_valid(output_path, clip.duration):
            output_path.unlink(missing_ok=True)
            raise PipelineError(
                errors.CLIP_EXTRACTION_FAILED,
                "The clip could not be extracted from the video.",
                technical_details=f"FFmpeg output failed validation: {output_path.name}",
                retryable=True,
            )
        if self.logger:
            self.logger.event("clip_extracted", mode="reencode", path=str(output_path))
        return output_path

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _copy_is_platform_safe(self, input_path: Path) -> bool:
        """Only stream-copy codecs that social platforms accept as-is."""
        codecs = stream_codecs(input_path, self.config.ffprobe_bin)
        video = [name for kind, name in codecs if kind == "video"]
        audio = [name for kind, name in codecs if kind == "audio"]
        return video == ["h264"] and all(name == "aac" for name in audio)

    def _base_command(self, input_path: Path, clip: ClipCandidate) -> list:
        return [
            self.config.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-ss", f"{clip.start_time:.3f}",
            "-i", str(input_path),
            "-t", f"{clip.duration:.3f}",
            "-map", "0:v:0",
            "-map", "0:a?",
        ]

    def _extract_copy(self, input_path: Path, clip: ClipCandidate, output_path: Path) -> None:
        cmd = self._base_command(input_path, clip) + [
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            "-y", str(output_path),
        ]
        try:
            run_command(cmd, timeout=max(120, int(clip.duration * 8)))
        except PipelineError:
            # The fallback is decided by the caller; an invalid/unsupported
            # stream combination should not abort the whole job here.
            return

    def _extract_reencode(self, input_path: Path, clip: ClipCandidate, output_path: Path) -> None:
        cmd = self._base_command(input_path, clip) + [
            "-c:v", "libx264",
            "-preset", self.config.clip_preset,
            "-crf", str(self.config.clip_crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-fps_mode", "vfr",
            "-y", str(output_path),
        ]
        try:
            run_command(cmd, timeout=max(180, int(clip.duration * 20)))
            return
        except PipelineError as exc:
            # Older FFmpeg builds lack -fps_mode. Retry with the legacy option.
            if "fps_mode" not in (exc.technical_details or "") and "Unrecognized option" not in (exc.technical_details or ""):
                raise
        legacy = list(cmd)
        legacy.remove("-fps_mode")
        legacy.remove("vfr")
        index = legacy.index("-y")
        legacy[index:index] = ["-vsync", "vfr"]
        run_command(legacy, timeout=max(180, int(clip.duration * 20)))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _existing_output_is_valid(self, path: Path, expected_duration: float) -> bool:
        if not path.exists() or path.stat().st_size < 10 * 1024:
            return False
        if not has_video_stream(path, self.config.ffprobe_bin):
            return False
        actual = probe_duration(path, self.config.ffprobe_bin)
        if actual is None:
            return True
        tolerance = max(1.0, min(4.0, expected_duration * 0.08))
        return abs(actual - expected_duration) <= tolerance
