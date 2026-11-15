"""Download adapter around the project's existing yt-dlp downloader.

The existing CLI/GUI downloader in ``clipper.downloader`` remains the
single implementation used for actual video downloads. This module adds
metadata, caching, retries, limits, and a typed interface on top of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Tuple

from clipper.downloader import download_full_video
from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError, redact
from clipper.viral.media import probe_duration, tool_available
from clipper.viral.models import DownloadedVideo, DownloadOptions
from clipper.viral.validation import check_disk_space

_CACHE_LOCK = threading.Lock()


class VideoDownloader(Protocol):
    def download(self, url: str, options: DownloadOptions) -> DownloadedVideo:
        ...


def _classify_ytdlp_error(text: str) -> Tuple[str, str, bool]:
    lower = (text or "").lower()
    if any(token in lower for token in ("private video", "sign in", "login", "members-only", "age-restricted", "confirm your age", "cookies")):
        return (
            errors.VIDEO_PRIVATE_OR_AUTH_REQUIRED,
            "This video is private, age-restricted, or requires a login.",
            False,
        )
    if "unsupported url" in lower or "no suitable extractor" in lower:
        return errors.UNSUPPORTED_URL, "This video URL is not supported by the downloader.", False
    if any(token in lower for token in ("video unavailable", "not available", "has been removed", "does not exist")):
        return errors.VIDEO_UNAVAILABLE, "This video is unavailable.", False
    return errors.DOWNLOAD_FAILED, "The video could not be downloaded.", True


class ExistingYtDlpDownloader:
    """VideoDownloader that reuses ``clipper.downloader.download_full_video``."""

    def __init__(self, config: ViralConfig, logger: Any = None) -> None:
        self.config = config
        self.logger = logger
        self.cache_dir = Path(config.cache_dir or (config.output_dir / ".cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "index.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download(self, url: str, options: DownloadOptions) -> DownloadedVideo:
        options.output_dir.mkdir(parents=True, exist_ok=True)
        options.metadata_dir.mkdir(parents=True, exist_ok=True)
        target = options.output_dir / "video.mp4"
        cache_key = self._cache_key(url, options.format)

        if self.logger:
            self.logger.started("download", url=url, cache_key=cache_key)

        if not options.force:
            cached = self._cache_lookup(cache_key)
            if cached:
                cached_path = Path(cached.get("path", ""))
                if cached_path.exists() and cached_path.stat().st_size > 50 * 1024:
                    self._link_or_copy(cached_path, target)
                    info = dict(cached.get("metadata") or {})
                    video = self._video_from_info(url, target, info, from_cache=True)
                    if video.duration > self.config.max_video_duration_seconds:
                        raise PipelineError(
                            errors.VIDEO_TOO_LONG,
                            "This video is longer than the configured maximum duration.",
                            technical_details=f"duration={video.duration}",
                        )
                    if self.logger:
                        self.logger.completed("download", from_cache=True, path=str(target))
                    return video

        if target.exists() and target.stat().st_size > 50 * 1024 and not options.force:
            info_path = options.metadata_dir / "source_info.json"
            if info_path.exists():
                try:
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                    video = self._video_from_info(url, target, info, from_cache=False)
                    if self.logger:
                        self.logger.completed("download", reused_existing=True, path=str(target))
                    return video
                except (OSError, json.JSONDecodeError):
                    pass

        info = self._fetch_info(url, options.timeout_seconds)
        self._guard_info(info)
        duration = float(info.get("duration") or 0)
        if duration > self.config.max_video_duration_seconds:
            raise PipelineError(
                errors.VIDEO_TOO_LONG,
                "This video is longer than the configured maximum duration.",
                technical_details=f"duration={duration} max={self.config.max_video_duration_seconds}",
            )

        estimated = int(info.get("filesize_approx") or info.get("filesize") or 0)
        required = estimated * 2 if estimated else 750 * 1024 * 1024
        check_disk_space(options.output_dir, required, self.config.min_free_space_mb)

        config_dict = {
            "format": options.format,
            "retry": 1,
            "min_file_size_kb": 50,
            "force_keyframes": False,
        }
        last_error = ""
        for attempt in range(1, max(1, options.max_retries) + 1):
            try:
                ok = download_full_video(url, target, config_dict)
            except Exception as exc:  # the legacy function may raise on odd IO errors
                ok = False
                last_error = str(exc)
            if ok and target.exists() and target.stat().st_size > 50 * 1024:
                break
            last_error = last_error or "downloader returned failure"
            self._cleanup_partials(options.output_dir)
            if attempt < options.max_retries:
                time.sleep(min(10, 2 ** attempt))
        else:
            code, message, retryable = _classify_ytdlp_error(last_error)
            raise PipelineError(
                code,
                message,
                technical_details=last_error or "download_full_video returned False",
                retryable=retryable,
            )

        size = target.stat().st_size
        if size > self.config.max_file_size_mb * 1024 * 1024:
            target.unlink(missing_ok=True)
            raise PipelineError(
                errors.FILE_TOO_LARGE,
                "The downloaded video exceeds the configured file size limit.",
                technical_details=f"size={size} max_mb={self.config.max_file_size_mb}",
            )

        probed = probe_duration(target, self.config.ffprobe_bin)
        actual_duration = float(probed or duration or 0)
        info["_resolved_duration"] = actual_duration
        info_path = options.metadata_dir / "source_info.json"
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        thumbnail = self._download_thumbnail(url, options.metadata_dir, options.timeout_seconds)
        if thumbnail:
            info["_thumbnail_path"] = str(thumbnail)
        video = self._video_from_info(url, target, info, from_cache=False)
        video.thumbnail = thumbnail
        self._cache_store(cache_key, target, info)

        if self.logger:
            self.logger.completed("download", path=str(target), size=size, duration=actual_duration)
        return video

    # ------------------------------------------------------------------
    # Metadata / cache
    # ------------------------------------------------------------------

    def _fetch_info(self, url: str, timeout: int) -> Dict[str, Any]:
        cmd = [
            self.config.ytdlp_bin, "-J", "--no-playlist", "--no-warnings",
            "--socket-timeout", "20", url,
        ]
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
                errors.CONFIGURATION_ERROR,
                "yt-dlp was not found. Install it or set YTDLP_BIN.",
                technical_details=str(exc),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(
                errors.TIMEOUT,
                "Fetching video metadata took too long.",
                technical_details=str(exc),
                retryable=True,
            ) from exc
        if result.returncode != 0:
            code, message, retryable = _classify_ytdlp_error(result.stderr or result.stdout)
            raise PipelineError(code, message, technical_details=result.stderr or result.stdout, retryable=retryable)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PipelineError(
                errors.DOWNLOAD_FAILED,
                "The video metadata could not be read.",
                technical_details=redact(result.stdout[-1000:]),
                retryable=True,
            ) from exc
        if not isinstance(data, dict):
            raise PipelineError(errors.DOWNLOAD_FAILED, "The video metadata is invalid.", retryable=True)
        return data

    def _guard_info(self, info: Dict[str, Any]) -> None:
        if info.get("_type") == "playlist" or info.get("entries"):
            raise PipelineError(
                errors.UNSUPPORTED_URL,
                "Please provide a single video URL, not a playlist or channel.",
            )
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise PipelineError(errors.VIDEO_UNAVAILABLE, "Live streams and premieres are not supported.")
        availability = str(info.get("availability") or "").lower()
        if availability in ("private", "premium_only", "subscriber_only", "needs_auth"):
            raise PipelineError(
                errors.VIDEO_PRIVATE_OR_AUTH_REQUIRED,
                "This video is private or requires a login.",
            )

    def _video_from_info(
        self,
        url: str,
        path: Path,
        info: Dict[str, Any],
        from_cache: bool,
    ) -> DownloadedVideo:
        duration = float(info.get("_resolved_duration") or info.get("duration") or 0)
        quality = "best_available"
        heights = []
        for fmt in info.get("formats") or []:
            if fmt.get("vcodec") not in (None, "none") and fmt.get("height"):
                heights.append(int(fmt["height"]))
        if heights:
            quality = f"{max(heights)}p"
        thumb_value = info.get("_thumbnail_path") or info.get("thumbnail")
        thumb = Path(thumb_value) if thumb_value and Path(str(thumb_value)).exists() else None
        return DownloadedVideo(
            path=path,
            title=str(info.get("title") or "Untitled video"),
            duration=duration,
            source_url=url,
            quality=quality,
            filesize=path.stat().st_size if path.exists() else 0,
            thumbnail=thumb,
            uploader=str(info.get("uploader") or info.get("channel") or ""),
            video_id=str(info.get("id") or ""),
            from_cache=from_cache,
            raw_metadata=info,
        )

    def _download_thumbnail(self, url: str, metadata_dir: Path, timeout: int) -> Optional[Path]:
        template = metadata_dir / "thumbnail"
        cmd = [
            self.config.ytdlp_bin,
            "--write-thumbnail", "--skip-download", "--no-playlist", "--no-warnings",
            "-o", str(template),
            url,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=min(timeout, 120), text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        candidates = sorted(metadata_dir.glob("thumbnail.*"))
        return candidates[0] if candidates else None

    def _cache_key(self, url: str, fmt: str) -> str:
        digest = hashlib.sha256(f"{url}|{fmt}".encode("utf-8")).hexdigest()
        return digest

    def _read_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_index(self, data: Dict[str, Any]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _cache_lookup(self, key: str) -> Optional[Dict[str, Any]]:
        with _CACHE_LOCK:
            entry = self._read_index().get(key)
        if not isinstance(entry, dict):
            return None
        path = Path(entry.get("path", ""))
        if not path.exists():
            return None
        return entry

    def _cache_store(self, key: str, path: Path, info: Dict[str, Any]) -> None:
        with _CACHE_LOCK:
            index = self._read_index()
            index[key] = {
                "path": str(path.resolve()),
                "cached_at": time.time(),
                "metadata": info,
            }
            self._write_index(index)

    def _link_or_copy(self, source: Path, target: Path) -> None:
        if target.exists() and target.stat().st_size > 50 * 1024:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.exists():
                target.unlink()
            os.link(source, target)
            return
        except OSError:
            pass
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            raise PipelineError(
                errors.DOWNLOAD_FAILED,
                "The cached video could not be copied into the job folder.",
                technical_details=str(exc),
                retryable=True,
            ) from exc

    def _cleanup_partials(self, directory: Path) -> None:
        for pattern in ("*_full_temp*", "*.part", "*.ytdl"):
            for path in directory.glob(pattern):
                try:
                    path.unlink()
                except OSError:
                    pass
