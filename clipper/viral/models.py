"""Data models shared by the viral pipeline modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SUPPORTED_PLATFORMS = ("tiktok", "instagram_reels", "youtube_shorts")
SUPPORTED_LANGUAGES = ("auto", "ar", "en")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobOptions:
    language: str = "auto"
    number_of_clips: int = 5
    min_clip_duration: float = 20.0
    max_clip_duration: float = 90.0
    platforms: List[str] = field(default_factory=lambda: list(SUPPORTED_PLATFORMS))
    download_original: bool = True
    generate_subtitles: bool = True
    download_format: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "number_of_clips": self.number_of_clips,
            "min_clip_duration": self.min_clip_duration,
            "max_clip_duration": self.max_clip_duration,
            "platforms": list(self.platforms),
            "download_original": self.download_original,
            "generate_subtitles": self.generate_subtitles,
            "download_format": self.download_format,
        }


@dataclass
class DownloadOptions:
    output_dir: Path
    metadata_dir: Path
    cache_dir: Path
    format: str = "bv*+ba/b"
    max_retries: int = 3
    timeout_seconds: int = 1800
    force: bool = False


@dataclass
class DownloadedVideo:
    path: Path
    title: str
    duration: float
    source_url: str
    quality: str = "best_available"
    filesize: int = 0
    thumbnail: Optional[Path] = None
    uploader: str = ""
    video_id: str = ""
    from_cache: bool = False
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    downloaded_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "duration": self.duration,
            "quality": self.quality,
            "filesize": self.filesize,
            "thumbnail": str(self.thumbnail) if self.thumbnail else None,
            "uploader": self.uploader,
            "video_id": self.video_id,
            "from_cache": self.from_cache,
        }


@dataclass
class TranscriptSegment:
    id: int
    start: float
    end: float
    text: str
    confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "confidence": self.confidence,
        }


@dataclass
class Transcript:
    language: str
    duration: float
    segments: List[TranscriptSegment]
    provider: str = "unknown"
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "language": self.language,
            "duration": round(self.duration, 3),
            "provider": self.provider,
            "segments": [s.to_dict() for s in self.segments],
        }
        if include_raw:
            data["raw"] = self.raw
        return data


@dataclass
class ClipCandidate:
    rank: int
    title: str
    hook: str
    start_time: float
    end_time: float
    duration: float
    viral_score: int
    category: str
    reason: str
    summary: str
    platforms: List[str]
    keywords: List[str]
    confidence: float = 0.0
    adjustments: List[str] = field(default_factory=list)
    text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank,
            "title": self.title,
            "hook": self.hook,
            "start_time": round(self.start_time, 3),
            "end_time": round(self.end_time, 3),
            "duration": round(self.duration, 3),
            "viral_score": self.viral_score,
            "category": self.category,
            "reason": self.reason,
            "summary": self.summary,
            "platforms": list(self.platforms),
            "keywords": list(self.keywords),
            "confidence": self.confidence,
            "adjustments": list(self.adjustments),
        }


@dataclass
class AnalysisResult:
    clips: List[Dict[str, Any]]
    provider: str
    model: str = ""
    attempts: int = 1
    raw_response: str = ""


@dataclass
class JobRecord:
    job_id: str
    source_url: str
    status: str = "queued"
    progress: float = 0.0
    current_step: str = "queued"
    error: Optional[Dict[str, Any]] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    output_path: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    result_path: Optional[str] = None
    package_path: Optional[str] = None
    report_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_url": self.source_url,
            "status": self.status,
            "progress": round(self.progress, 1),
            "current_step": self.current_step,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "output_path": self.output_path,
            "options": self.options,
            "attempts": self.attempts,
            "result_path": self.result_path,
            "package_path": self.package_path,
            "report_path": self.report_path,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobRecord":
        return cls(
            job_id=data["job_id"],
            source_url=data.get("source_url", ""),
            status=data.get("status", "queued"),
            progress=float(data.get("progress", 0) or 0),
            current_step=data.get("current_step", "queued"),
            error=data.get("error"),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
            output_path=data.get("output_path", ""),
            options=data.get("options", {}) or {},
            attempts=int(data.get("attempts", 1) or 1),
            result_path=data.get("result_path"),
            package_path=data.get("package_path"),
            report_path=data.get("report_path"),
        )


@dataclass
class PipelineResult:
    result: Dict[str, Any]
    output_dir: Path
    result_path: Path
    report_path: Path
    package_path: Path


def normalize_platforms(values: Sequence[str]) -> List[str]:
    return [v for v in values if v in SUPPORTED_PLATFORMS]
