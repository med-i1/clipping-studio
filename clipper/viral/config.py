"""Environment-driven configuration for the viral clip pipeline."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def _default_project_root() -> Path:
    """Writable application root (the folder containing the EXE when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def _bundled_resource(*parts: str) -> Path:
    """Read-only bundled resource directory in a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", _default_project_root()))
    return base.joinpath(*parts)


PROJECT_ROOT = _default_project_root()
DEFAULT_PROMPT_FILE = (
    _bundled_resource("prompts", "viral_clip_analysis.txt")
    if getattr(sys, "frozen", False)
    else PROJECT_ROOT / "prompts" / "viral_clip_analysis.txt"
)

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_PROVIDER_ALIASES = ("google", "gemini", "google_gemini")


def _read_dotenv(path: Path) -> Dict[str, str]:
    """Read a small KEY=VALUE .env file without external dependencies."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _env_bool(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _env_float(value: Optional[str], default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class ViralConfig:
    project_root: Path = PROJECT_ROOT
    output_dir: Path = PROJECT_ROOT / "Clipping" / "output" / "viral"
    temp_dir: Path = PROJECT_ROOT / "Clipping" / "tmp"
    cache_dir: Optional[Path] = None
    prompt_file: Path = DEFAULT_PROMPT_FILE

    ytdlp_bin: str = "yt-dlp"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    download_format: str = "bv*+ba/b"
    download_timeout_seconds: int = 1800
    max_video_duration_seconds: int = 14400
    max_file_size_mb: int = 4096
    min_free_space_mb: int = 500
    max_retries: int = 3
    max_clips: int = 10
    default_min_clip_duration: float = 20.0
    default_max_clip_duration: float = 90.0
    job_workers: int = 1

    llm_provider: str = "auto"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout_seconds: int = 180
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    llm_json_mode: bool = True
    llm_max_transcript_chars: int = 60000
    llm_max_chunks: int = 12

    transcription_provider: str = "auto"
    transcription_model: str = "whisper-1"
    transcription_api_key: str = ""
    transcription_base_url: str = ""
    transcription_language: str = "auto"
    stt_chunk_seconds: int = 600
    keep_audio: bool = False
    local_whisper_model: str = "small"

    clip_copy_mode: str = "auto"  # auto | copy | reencode
    clip_crf: int = 18
    clip_preset: str = "veryfast"

    snap_tolerance_seconds: float = 2.0
    max_overlap_seconds: float = 1.5
    max_clip_overlap_ratio: float = 0.15

    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_token: str = ""
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.cache_dir is None:
            self.cache_dir = self.output_dir / ".cache"
        if not self.transcription_base_url:
            self.transcription_base_url = self.llm_base_url
        if not self.transcription_api_key:
            self.transcription_api_key = self.llm_api_key

    def ensure_dirs(self) -> None:
        for path in (self.output_dir, self.temp_dir, self.cache_dir, self.prompt_file.parent):
            path.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> Dict[str, Any]:
        """Configuration safe to include in reports (no secrets)."""
        return {
            "output_dir": str(self.output_dir),
            "temp_dir": str(self.temp_dir),
            "download_format": self.download_format,
            "max_video_duration_seconds": self.max_video_duration_seconds,
            "max_file_size_mb": self.max_file_size_mb,
            "max_retries": self.max_retries,
            "max_clips": self.max_clips,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "transcription_provider": self.transcription_provider,
            "transcription_model": self.transcription_model,
            "clip_copy_mode": self.clip_copy_mode,
        }


def load_config(
    env: Optional[Mapping[str, str]] = None,
    project_root: Optional[Path] = None,
) -> ViralConfig:
    """Load config from .env plus process environment.

    Values already present in the process environment win over .env values.
    Passing ``env`` makes the call deterministic for tests and skips .env.
    """
    root = Path(project_root) if project_root else PROJECT_ROOT
    if env is None:
        merged: Dict[str, str] = {}
        merged.update(_read_dotenv(root / ".env"))
        merged.update(os.environ)
    else:
        merged = {str(k): str(v) for k, v in env.items()}

    output_dir = Path(merged.get("VIRAL_OUTPUT_DIR") or merged.get("OUTPUT_DIR") or (root / "Clipping" / "output" / "viral"))
    if not output_dir.is_absolute():
        output_dir = (root / output_dir).resolve()
    temp_dir = Path(merged.get("VIRAL_TEMP_DIR") or merged.get("TEMP_DIR") or (root / "Clipping" / "tmp"))
    if not temp_dir.is_absolute():
        temp_dir = (root / temp_dir).resolve()
    cache_value = merged.get("VIRAL_CACHE_DIR", "")
    cache_dir = Path(cache_value) if cache_value else None
    if cache_dir is not None and not cache_dir.is_absolute():
        cache_dir = (root / cache_dir).resolve()

    prompt_value = merged.get("LLM_PROMPT_FILE", "")
    if prompt_value:
        prompt_file = Path(prompt_value)
    elif getattr(sys, "frozen", False):
        prompt_file = DEFAULT_PROMPT_FILE
    else:
        prompt_file = root / "prompts" / "viral_clip_analysis.txt"
    if not prompt_file.is_absolute():
        prompt_file = (root / prompt_file).resolve()

    llm_provider = merged.get("LLM_PROVIDER", "auto").strip().lower() or "auto"
    if llm_provider in GEMINI_PROVIDER_ALIASES:
        llm_provider = "gemini"
    llm_model = (merged.get("LLM_MODEL") or "").strip()
    llm_base_url = (merged.get("LLM_BASE_URL") or "").strip().rstrip("/")
    if llm_provider == "gemini":
        # Google AI Studio keys work through Gemini's OpenAI-compatible surface.
        if not llm_base_url or llm_base_url == OPENAI_BASE_URL:
            llm_base_url = GEMINI_BASE_URL
        if not llm_model or llm_model == OPENAI_DEFAULT_MODEL:
            llm_model = GEMINI_DEFAULT_MODEL
    else:
        if not llm_base_url:
            llm_base_url = OPENAI_BASE_URL
        if not llm_model:
            llm_model = OPENAI_DEFAULT_MODEL

    cfg = ViralConfig(
        project_root=root,
        output_dir=output_dir,
        temp_dir=temp_dir,
        cache_dir=cache_dir,
        prompt_file=prompt_file,
        ytdlp_bin=merged.get("YTDLP_BIN", "yt-dlp"),
        ffmpeg_bin=merged.get("FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=merged.get("FFPROBE_BIN", "ffprobe"),
        download_format=merged.get("DOWNLOAD_FORMAT", "bv*+ba/b"),
        download_timeout_seconds=_env_int(merged.get("DOWNLOAD_TIMEOUT_SECONDS"), 1800),
        max_video_duration_seconds=_env_int(merged.get("MAX_VIDEO_DURATION_SECONDS"), 14400),
        max_file_size_mb=_env_int(merged.get("MAX_FILE_SIZE_MB"), 4096),
        min_free_space_mb=_env_int(merged.get("MIN_FREE_SPACE_MB"), 500),
        max_retries=max(1, _env_int(merged.get("MAX_RETRIES"), 3)),
        max_clips=max(1, _env_int(merged.get("MAX_CLIPS"), 10)),
        default_min_clip_duration=_env_float(merged.get("MIN_CLIP_DURATION"), 20.0),
        default_max_clip_duration=_env_float(merged.get("MAX_CLIP_DURATION"), 90.0),
        job_workers=max(1, _env_int(merged.get("JOB_WORKERS"), 1)),
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=merged.get("LLM_API_KEY", ""),
        llm_base_url=llm_base_url,
        llm_timeout_seconds=_env_int(merged.get("LLM_TIMEOUT_SECONDS"), 180),
        llm_max_retries=max(0, _env_int(merged.get("LLM_MAX_RETRIES"), 2)),
        llm_temperature=_env_float(merged.get("LLM_TEMPERATURE"), 0.2),
        llm_json_mode=_env_bool(merged.get("LLM_JSON_MODE"), True),
        llm_max_transcript_chars=max(4000, _env_int(merged.get("LLM_MAX_TRANSCRIPT_CHARS"), 60000)),
        llm_max_chunks=max(1, _env_int(merged.get("LLM_MAX_CHUNKS"), 12)),
        transcription_provider=(merged.get("TRANSCRIPTION_PROVIDER", "auto").strip().lower() or "auto"),
        transcription_model=merged.get("TRANSCRIPTION_MODEL", "whisper-1"),
        transcription_api_key=merged.get("TRANSCRIPTION_API_KEY", ""),
        transcription_base_url=(merged.get("TRANSCRIPTION_BASE_URL") or "").rstrip("/"),
        transcription_language=merged.get("TRANSCRIPTION_LANGUAGE", "auto").strip() or "auto",
        stt_chunk_seconds=max(30, _env_int(merged.get("STT_CHUNK_SECONDS"), 600)),
        keep_audio=_env_bool(merged.get("KEEP_AUDIO"), False),
        local_whisper_model=merged.get("LOCAL_WHISPER_MODEL", "small"),
        clip_copy_mode=(merged.get("CLIP_COPY_MODE", "auto").strip().lower() or "auto"),
        clip_crf=_env_int(merged.get("CLIP_CRF"), 18),
        clip_preset=merged.get("CLIP_PRESET", "veryfast"),
        snap_tolerance_seconds=_env_float(merged.get("SNAP_TOLERANCE_SECONDS"), 2.0),
        max_overlap_seconds=_env_float(merged.get("MAX_OVERLAP_SECONDS"), 1.5),
        max_clip_overlap_ratio=_env_float(merged.get("MAX_CLIP_OVERLAP_RATIO"), 0.15),
        api_host=merged.get("VIRAL_API_HOST", "127.0.0.1"),
        api_port=_env_int(merged.get("VIRAL_API_PORT"), 8765),
        api_token=merged.get("VIRAL_API_TOKEN", ""),
        log_level=merged.get("LOG_LEVEL", "INFO").upper(),
    )
    if cfg.default_min_clip_duration <= 0:
        cfg.default_min_clip_duration = 20.0
    if cfg.default_max_clip_duration < cfg.default_min_clip_duration:
        cfg.default_max_clip_duration = cfg.default_min_clip_duration
    return cfg
