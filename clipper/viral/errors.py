"""Typed, user-facing errors for the viral pipeline."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_SECRET_PATTERNS = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)[^\s&,;]+"),
)


def redact(text: Optional[str]) -> str:
    """Remove obvious credentials from text that may reach logs or clients."""
    if not text:
        return ""
    cleaned = str(text)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 1:
            cleaned = pattern.sub(r"\1[REDACTED]", cleaned)
        elif pattern.groups == 2:
            cleaned = pattern.sub(r"\1[REDACTED]", cleaned)
        else:
            cleaned = pattern.sub(r"\1[REDACTED]", cleaned)
    return cleaned[:4000]


class PipelineError(Exception):
    """An error with a stable machine code and a safe public message."""

    def __init__(
        self,
        code: str,
        message: str,
        technical_details: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.technical_details = redact(technical_details)
        self.retryable = retryable

    def to_dict(self, include_details: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if include_details:
            data["technical_details"] = self.technical_details
        return data

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


# Stable error codes. Keep these in one place so clients and tests can rely
# on them without importing individual modules.
INVALID_URL = "INVALID_URL"
UNSUPPORTED_URL = "UNSUPPORTED_URL"
INVALID_OPTIONS = "INVALID_OPTIONS"
UNSAFE_PATH = "UNSAFE_PATH"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
NETWORK_ERROR = "NETWORK_ERROR"
VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
VIDEO_PRIVATE_OR_AUTH_REQUIRED = "VIDEO_PRIVATE_OR_AUTH_REQUIRED"
VIDEO_TOO_LONG = "VIDEO_TOO_LONG"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
INSUFFICIENT_STORAGE = "INSUFFICIENT_STORAGE"
NO_AUDIO_STREAM = "NO_AUDIO_STREAM"
AUDIO_EXTRACTION_FAILED = "AUDIO_EXTRACTION_FAILED"
TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
TRANSCRIPTION_UNAVAILABLE = "TRANSCRIPTION_UNAVAILABLE"
ANALYSIS_FAILED = "ANALYSIS_FAILED"
ANALYSIS_INVALID_JSON = "ANALYSIS_INVALID_JSON"
NO_VALID_CLIPS = "NO_VALID_CLIPS"
FFMPEG_NOT_FOUND = "FFMPEG_NOT_FOUND"
CLIP_EXTRACTION_FAILED = "CLIP_EXTRACTION_FAILED"
PACKAGING_FAILED = "PACKAGING_FAILED"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
JOB_NOT_RETRYABLE = "JOB_NOT_RETRYABLE"
JOB_NOT_DELETABLE = "JOB_NOT_DELETABLE"
TIMEOUT = "TIMEOUT"
INTERNAL_ERROR = "INTERNAL_ERROR"
