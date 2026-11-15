"""Input, filename, path, and option validation."""

from __future__ import annotations

import ipaddress
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlparse

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.models import JobOptions, SUPPORTED_PLATFORMS

_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_LANGUAGE_RE = re.compile(r"^(auto|[a-z]{2,3}(?:-[a-z0-9]{2,8})?)$")
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_url(url: Any) -> str:
    """Validate a public http(s) URL and return it normalized.

    This is also the SSRF gate for the optional local API: file URLs,
    localhost, single-label hosts, and private/loopback IP literals are
    rejected before yt-dlp sees them.
    """
    if not isinstance(url, str):
        raise PipelineError(errors.INVALID_URL, "The video URL must be text.")
    value = url.strip()
    if not value:
        raise PipelineError(errors.INVALID_URL, "The video URL is empty.")
    if len(value) > 2048:
        raise PipelineError(errors.INVALID_URL, "The video URL is too long.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise PipelineError(errors.INVALID_URL, "The video URL contains control characters.")
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise PipelineError(errors.INVALID_URL, "The video URL is malformed.") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise PipelineError(
            errors.UNSUPPORTED_URL,
            "Only http:// and https:// video URLs are supported.",
        )
    if not parsed.netloc or not parsed.hostname:
        raise PipelineError(errors.INVALID_URL, "The video URL has no host.")
    if parsed.username or parsed.password:
        raise PipelineError(errors.INVALID_URL, "URLs containing credentials are not allowed.")
    host = parsed.hostname.strip().rstrip(".").lower()
    if not host:
        raise PipelineError(errors.INVALID_URL, "The video URL has no host.")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost") or host.endswith(".local"):
        raise PipelineError(errors.UNSUPPORTED_URL, "Local network URLs are not supported.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise PipelineError(errors.UNSUPPORTED_URL, "Private or local IP addresses are not supported.")
    elif "." not in host:
        raise PipelineError(errors.UNSUPPORTED_URL, "The URL host is not a public domain name.")
    try:
        _ = parsed.port  # raises ValueError on an invalid port
    except ValueError as exc:
        raise PipelineError(errors.INVALID_URL, "The video URL has an invalid port.") from exc
    return value


def validate_job_id(job_id: str) -> str:
    value = (job_id or "").strip().lower()
    if not _JOB_ID_RE.fullmatch(value):
        raise PipelineError(errors.JOB_NOT_FOUND, "Job not found.")
    return value


def safe_filename(value: Any, fallback: str = "file", max_len: int = 120) -> str:
    """Make a single path-safe filename component.

    Keeps Unicode (including Arabic), removes separators and control
    characters, avoids Windows reserved names and trailing dots/spaces.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _INVALID_FILENAME_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = "_" + text
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    return text or fallback


def safe_component(value: Any, fallback: str = "file", max_len: int = 120) -> str:
    """A filename component with no directory component at all."""
    name = Path(str(value or "")).name
    return safe_filename(name, fallback=fallback, max_len=max_len)


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve candidate and reject escapes from root (path traversal)."""
    root_resolved = Path(root).resolve()
    candidate_resolved = Path(candidate).resolve()
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PipelineError(errors.UNSAFE_PATH, "The requested path is outside the job folder.") from exc
    return candidate_resolved


def validate_job_options(
    payload: Optional[Mapping[str, Any]],
    config: ViralConfig,
) -> JobOptions:
    """Validate the ``options`` object from CLI/API input."""
    data: Dict[str, Any] = dict(payload or {})
    if not isinstance(payload, Mapping):
        raise PipelineError(errors.INVALID_OPTIONS, "Options must be a JSON object.")

    language = str(data.get("language", "auto") or "auto").strip().lower()
    if not _LANGUAGE_RE.fullmatch(language):
        raise PipelineError(
            errors.INVALID_OPTIONS,
            "language must be 'auto' or a language code such as 'ar' or 'en'.",
        )

    number_raw = data.get("number_of_clips", 5)
    if isinstance(number_raw, bool):
        raise PipelineError(errors.INVALID_OPTIONS, "number_of_clips must be an integer.")
    try:
        number = int(number_raw)
    except (TypeError, ValueError) as exc:
        raise PipelineError(errors.INVALID_OPTIONS, "number_of_clips must be an integer.") from exc
    if str(number_raw).strip() not in (str(number),) and not isinstance(number_raw, (int, float)):
        raise PipelineError(errors.INVALID_OPTIONS, "number_of_clips must be an integer.")
    if number < 1 or number > config.max_clips:
        raise PipelineError(
            errors.INVALID_OPTIONS,
            f"number_of_clips must be between 1 and {config.max_clips}.",
        )

    def _duration(name: str, default: float) -> float:
        raw = data.get(name, default)
        if isinstance(raw, bool):
            raise PipelineError(errors.INVALID_OPTIONS, f"{name} must be a number.")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise PipelineError(errors.INVALID_OPTIONS, f"{name} must be a number.") from exc
        if value <= 0 or value > 3600:
            raise PipelineError(errors.INVALID_OPTIONS, f"{name} is outside a sensible range.")
        return value

    min_duration = _duration("min_clip_duration", config.default_min_clip_duration)
    max_duration = _duration("max_clip_duration", config.default_max_clip_duration)
    if min_duration < 5:
        raise PipelineError(errors.INVALID_OPTIONS, "min_clip_duration must be at least 5 seconds.")
    if max_duration < min_duration:
        raise PipelineError(errors.INVALID_OPTIONS, "max_clip_duration must be >= min_clip_duration.")

    raw_platforms = data.get("platforms", list(SUPPORTED_PLATFORMS))
    if isinstance(raw_platforms, str):
        raw_platforms = [raw_platforms]
    if not isinstance(raw_platforms, Iterable) or isinstance(raw_platforms, (bytes, Mapping)):
        raise PipelineError(errors.INVALID_OPTIONS, "platforms must be a list.")
    platforms = []
    for item in raw_platforms:
        platform = str(item).strip().lower()
        if platform not in SUPPORTED_PLATFORMS:
            raise PipelineError(
                errors.INVALID_OPTIONS,
                f"Unsupported platform '{platform}'. Use: {', '.join(SUPPORTED_PLATFORMS)}.",
            )
        if platform not in platforms:
            platforms.append(platform)
    if not platforms:
        raise PipelineError(errors.INVALID_OPTIONS, "At least one platform is required.")

    def _bool(name: str, default: bool) -> bool:
        raw = data.get(name, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return raw.strip().lower() in ("true", "1", "yes")
        raise PipelineError(errors.INVALID_OPTIONS, f"{name} must be true or false.")

    download_format = data.get("download_format")
    if download_format is not None:
        download_format = str(download_format).strip()
        if not download_format or len(download_format) > 200 or any(c in download_format for c in "\r\n\x00"):
            raise PipelineError(errors.INVALID_OPTIONS, "download_format is invalid.")
        download_format = download_format or None

    return JobOptions(
        language=language,
        number_of_clips=number,
        min_clip_duration=min_duration,
        max_clip_duration=max_duration,
        platforms=platforms,
        download_original=_bool("download_original", True),
        generate_subtitles=_bool("generate_subtitles", True),
        download_format=download_format,
    )


def check_disk_space(path: Path, required_bytes: int, min_free_mb: int) -> None:
    try:
        free = shutil.disk_usage(str(path)).free
    except OSError:
        return
    reserve = max(0, min_free_mb) * 1024 * 1024
    if free < required_bytes + reserve:
        raise PipelineError(
            errors.INSUFFICIENT_STORAGE,
            "There is not enough free disk space to process this video.",
            technical_details=f"required={required_bytes} free={free} reserve={reserve}",
            retryable=False,
        )
