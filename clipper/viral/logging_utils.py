"""Structured JSONL logging with per-stage timing."""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from clipper.viral.errors import redact


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JobLogger:
    """Writes one JSON object per line plus a compact console line.

    The file is always job-scoped (``logs/job.jsonl``) and safe for
    concurrent writers inside one process.
    """

    def __init__(self, job_id: str, log_path: Path, console: bool = True) -> None:
        self.job_id = job_id
        self.log_path = log_path
        self.console = console
        self._lock = threading.Lock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stage_started: Dict[str, float] = {}

    def _write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if self.console:
                stage = record.get("stage", "")
                level = record.get("level", "INFO")
                detail = record.get("message") or record.get("detail") or ""
                print(f"[{self.job_id[:8]}] {level} {stage} {detail}".rstrip(), file=sys.stderr)

    def event(self, stage: str, level: str = "INFO", **fields: Any) -> None:
        record: Dict[str, Any] = {
            "job_id": self.job_id,
            "timestamp": _now(),
            "stage": stage,
            "level": level,
        }
        for key, value in fields.items():
            if value is None:
                continue
            if key in ("error", "technical_details", "message"):
                value = redact(str(value))
            record[key] = value
        self._write(record)

    def started(self, stage: str, **fields: Any) -> None:
        self._stage_started[stage] = time.monotonic()
        self.event(f"{stage}_started", **fields)

    def completed(self, stage: str, **fields: Any) -> None:
        started = self._stage_started.pop(stage, None)
        if started is not None:
            fields.setdefault("duration_seconds", round(time.monotonic() - started, 3))
        self.event(f"{stage}_completed", **fields)

    def failed(self, stage: str, error: BaseException, **fields: Any) -> None:
        started = self._stage_started.pop(stage, None)
        if started is not None:
            fields.setdefault("duration_seconds", round(time.monotonic() - started, 3))
        fields["error"] = str(error)
        self.event(f"{stage}_failed", level="ERROR", **fields)


class NullLogger:
    """No-op logger used by tests and dry runs."""

    def event(self, stage: str, level: str = "INFO", **fields: Any) -> None:
        pass

    def started(self, stage: str, **fields: Any) -> None:
        pass

    def completed(self, stage: str, **fields: Any) -> None:
        pass

    def failed(self, stage: str, error: BaseException, **fields: Any) -> None:
        pass
