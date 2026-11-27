"""Disk-backed asynchronous job manager (no external queue required)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.logging_utils import JobLogger
from clipper.viral.models import JobOptions, JobRecord, utc_now
from clipper.viral.packaging import create_job_dirs, read_json, write_json
from clipper.viral.pipeline import build_pipeline
from clipper.viral.validation import (
    ensure_within,
    validate_job_id,
    validate_job_options,
    validate_url,
)

ACTIVE_STATUSES = {
    "queued", "downloading", "transcribing", "analyzing",
    "validating", "extracting_clips", "packaging",
}
TERMINAL_STATUSES = {"completed", "failed"}


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if a process with this PID is running.

    Never sends a signal on Windows: ``os.kill(pid, 0)`` there terminates
    the process instead of probing it.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied means the process exists but belongs to someone else.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class JobManager:
    """A small in-process worker plus JSON job records on disk.

    This deliberately avoids a hard Redis/Celery dependency while keeping
    the state machine and retry API compatible with a future queue.
    """

    def __init__(self, config: ViralConfig) -> None:
        self.config = config
        self.config.ensure_dirs()
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, config.job_workers),
            thread_name_prefix="viral-worker",
        )
        self._lock = threading.RLock()
        self._records: Dict[str, JobRecord] = {}
        self._futures: Dict[str, Future] = {}
        self.recover_interrupted_jobs()

    # ------------------------------------------------------------------
    # Creation / execution
    # ------------------------------------------------------------------

    def create_job(
        self,
        source_url: str,
        options: Optional[Dict[str, Any]] = None,
        submit: bool = True,
    ) -> JobRecord:
        url = validate_url(source_url)
        job_options: JobOptions = validate_job_options(options or {}, self.config)
        job_id = uuid.uuid4().hex
        created = utc_now()
        job_root = ensure_within(self.config.output_dir, self.config.output_dir / job_id)
        create_job_dirs(job_root)
        record = JobRecord(
            job_id=job_id,
            source_url=url,
            status="queued",
            progress=0,
            current_step="queued",
            created_at=created,
            updated_at=created,
            output_path=str(job_root),
            options=job_options.to_dict(),
            attempts=1,
            worker_pid=os.getpid(),
        )
        with self._lock:
            self._records[job_id] = record
        self._save(record)
        logger = JobLogger(job_id, job_root / "logs" / "job.jsonl")
        logger.event("job_created", url=url, options=job_options.to_dict())
        if submit:
            self._submit(record, logger)
        return record

    def _submit(self, record: JobRecord, logger: JobLogger) -> None:
        with self._lock:
            existing = self._futures.get(record.job_id)
            if existing is not None and not existing.done():
                raise PipelineError(
                    errors.JOB_NOT_RETRYABLE,
                    "This job is already running.",
                    retryable=False,
                )
            future = self.executor.submit(self._execute, record, logger)
            self._futures[record.job_id] = future

    def _execute(self, record: JobRecord, logger: JobLogger) -> None:
        with self._lock:
            record.worker_pid = os.getpid()
        try:
            pipeline = build_pipeline(self.config, logger)
            self._change(record, "downloading", 2, "starting")
            outcome = pipeline.run(
                job_id=record.job_id,
                source_url=record.source_url,
                options=JobOptions(**record.options),
                created_at=record.created_at,
                progress_callback=lambda value, step, detail: self._change(record, step, value, detail),
                logger=logger,
                job_root=Path(record.output_path),
            )
            with self._lock:
                record.status = "completed"
                record.progress = 100.0
                record.current_step = "completed"
                record.updated_at = utc_now()
                record.result_path = str(outcome.result_path)
                record.report_path = str(outcome.report_path)
                record.package_path = str(outcome.package_path)
                record.error = None
                self._save(record)
        except PipelineError as exc:
            self._fail(record, logger, exc)
        except Exception as exc:  # defensive: a worker must never die silently
            self._fail(
                record,
                logger,
                PipelineError(
                    errors.INTERNAL_ERROR,
                    "An unexpected error interrupted the job.",
                    technical_details=str(exc),
                    retryable=True,
                ),
            )
        finally:
            with self._lock:
                self._futures.pop(record.job_id, None)

    def execute_job(self, job_id: str) -> JobRecord:
        """Run a queued job synchronously in this process (detached worker)."""
        record = self.get_job(job_id)
        if record.status == "completed":
            raise PipelineError(errors.JOB_NOT_RETRYABLE, "This job has already completed.")
        with self._lock:
            # get_job returns a defensive copy; register the instance that
            # _execute will mutate so later reads do not see stale state.
            self._records[record.job_id] = record
        job_root = Path(record.output_path)
        create_job_dirs(job_root)
        logger = JobLogger(record.job_id, job_root / "logs" / "job.jsonl")
        logger.event("job_worker_started", attempt=record.attempts, pid=os.getpid())
        self._execute(record, logger)
        return self.get_job(record.job_id)

    def _fail(self, record: JobRecord, logger: JobLogger, exc: PipelineError) -> None:
        with self._lock:
            record.status = "failed"
            record.current_step = "failed"
            record.updated_at = utc_now()
            record.error = exc.to_dict(include_details=False)
            self._save(record)
        logger.event(
            "job_failed",
            level="ERROR",
            error=str(exc),
            code=exc.code,
            technical_details=exc.technical_details or None,
        )

    def _change(self, record: JobRecord, step: str, progress: float, detail: str = "") -> None:
        status_map = {
            "starting": "queued",
            "downloading": "downloading",
            "transcribing": "transcribing",
            "analyzing": "analyzing",
            "validating": "validating",
            "extracting_clips": "extracting_clips",
            "packaging": "packaging",
            # The pipeline emits "completed" just before it returns; the
            # worker still has to attach the result paths. Keep the job
            # active until _execute finalizes it so waiters never observe a
            # completed record without its artifacts.
            "completed": "packaging",
        }
        with self._lock:
            record.progress = max(0.0, min(100.0, float(progress)))
            record.current_step = step
            record.status = status_map.get(step, record.status)
            record.updated_at = utc_now()
            self._save(record)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> JobRecord:
        job_id = validate_job_id(job_id)
        with self._lock:
            record = self._records.get(job_id)
            if record is not None:
                return JobRecord.from_dict(record.to_dict())
        path = self._record_path(job_id)
        data = read_json(path)
        if not isinstance(data, dict):
            raise PipelineError(errors.JOB_NOT_FOUND, "Job not found.")
        record = JobRecord.from_dict(data)
        with self._lock:
            self._records[job_id] = record
        return JobRecord.from_dict(record.to_dict())

    def list_jobs(self, limit: int = 50) -> List[JobRecord]:
        if not self.config.output_dir.exists():
            return []
        records: List[JobRecord] = []
        for child in self.config.output_dir.iterdir():
            if not child.is_dir():
                continue
            data = read_json(child / "metadata" / "job.json")
            if isinstance(data, dict) and data.get("job_id"):
                try:
                    records.append(JobRecord.from_dict(data))
                except (KeyError, TypeError, ValueError):
                    continue
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records[:max(1, limit)]

    def retry_job(self, job_id: str, submit: bool = True) -> JobRecord:
        record = self._reconcile(self.get_job(job_id))
        if record.status in ACTIVE_STATUSES:
            raise PipelineError(errors.JOB_NOT_RETRYABLE, "This job is still running.")
        if record.status == "completed":
            raise PipelineError(errors.JOB_NOT_RETRYABLE, "Completed jobs do not need a retry.")
        job_root = Path(record.output_path)
        create_job_dirs(job_root)
        with self._lock:
            record.status = "queued"
            record.progress = 0.0
            record.current_step = "queued"
            record.error = None
            record.attempts += 1
            record.worker_pid = os.getpid()
            record.updated_at = utc_now()
            self._records[record.job_id] = record
            self._save(record)
        logger = JobLogger(record.job_id, job_root / "logs" / "job.jsonl")
        logger.event("job_retried", attempt=record.attempts)
        if submit:
            self._submit(record, logger)
        return record

    def delete_job(self, job_id: str) -> None:
        """Remove a finished job record and its output folder."""
        job_id = validate_job_id(job_id)
        with self._lock:
            record = self._reconcile(self.get_job(job_id))
            future = self._futures.get(job_id)
            if record.status in ACTIVE_STATUSES or (future is not None and not future.done()):
                raise PipelineError(
                    errors.JOB_NOT_DELETABLE,
                    "This job is still running. Wait for it to finish before deleting.",
                )
            job_root = ensure_within(self.config.output_dir, Path(record.output_path))
            if job_root.exists():
                try:
                    shutil.rmtree(job_root)
                except OSError as exc:
                    raise PipelineError(
                        errors.INTERNAL_ERROR,
                        "The job files could not be deleted.",
                        technical_details=str(exc),
                    ) from exc
            self._records.pop(job_id, None)
            self._futures.pop(job_id, None)

    def assign_worker(self, job_id: str, pid: int) -> JobRecord:
        """Record that a detached worker process now owns a queued job."""
        record = self.get_job(job_id)
        with self._lock:
            record.worker_pid = int(pid)
            record.updated_at = utc_now()
            self._records[record.job_id] = record
            self._save(record)
        return JobRecord.from_dict(record.to_dict())

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def _is_orphaned(self, record: JobRecord) -> bool:
        """An active job whose owning process is gone and will never finish it."""
        if record.status not in ACTIVE_STATUSES:
            return False
        with self._lock:
            future = self._futures.get(record.job_id)
            if future is not None and not future.done():
                return False
        if record.worker_pid == os.getpid():
            # Owned by this process (e.g. queued and waiting for a detached
            # worker to be spawned); not abandoned.
            return False
        return not _pid_alive(record.worker_pid)

    def _reconcile(self, record: JobRecord) -> JobRecord:
        """Mark an orphaned job as failed so it can be retried or deleted."""
        if not self._is_orphaned(record):
            return record
        interrupted_step = record.current_step
        with self._lock:
            record.status = "failed"
            record.current_step = "failed"
            record.updated_at = utc_now()
            record.error = PipelineError(
                errors.JOB_INTERRUPTED,
                "The job stopped before it finished because the app was closed "
                "or crashed. Retry it to run it again.",
                retryable=True,
            ).to_dict(include_details=False)
            self._records[record.job_id] = record
            self._save(record)
        if record.output_path:
            JobLogger(record.job_id, Path(record.output_path) / "logs" / "job.jsonl").event(
                "job_interrupted",
                level="WARNING",
                step=interrupted_step,
                worker_pid=record.worker_pid,
            )
        return JobRecord.from_dict(record.to_dict())

    def recover_interrupted_jobs(self) -> List[str]:
        """Fail every job left active by a process that no longer exists.

        Returns the IDs of the jobs that were recovered.
        """
        recovered: List[str] = []
        if not self.config.output_dir.exists():
            return recovered
        for child in self.config.output_dir.iterdir():
            if not child.is_dir():
                continue
            data = read_json(child / "metadata" / "job.json")
            if not isinstance(data, dict) or not data.get("job_id"):
                continue
            try:
                record = JobRecord.from_dict(data)
                validate_job_id(record.job_id)
            except (KeyError, TypeError, ValueError, PipelineError):
                continue
            if self._is_orphaned(record):
                try:
                    self._reconcile(record)
                except (OSError, PipelineError):
                    continue
                recovered.append(record.job_id)
        return recovered

    def wait(self, job_id: str, timeout: Optional[float] = None) -> JobRecord:
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            record = self.get_job(job_id)
            if record.status in TERMINAL_STATUSES:
                return record
            if deadline is not None and time.monotonic() >= deadline:
                raise PipelineError(errors.TIMEOUT, "Timed out waiting for the job to finish.", retryable=True)
            time.sleep(0.5)

    def shutdown(self, wait: bool = False) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=False)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _record_path(self, job_id: str) -> Path:
        job_root = ensure_within(self.config.output_dir, self.config.output_dir / job_id)
        return job_root / "metadata" / "job.json"

    def _save(self, record: JobRecord) -> None:
        job_root = Path(record.output_path)
        write_json(job_root / "metadata" / "job.json", record.to_dict())


def spawn_detached_worker(config: ViralConfig, job_id: str) -> subprocess.Popen:
    """Start a separate process to run a queued job.

    Used by the CLI so ``--no-wait`` can really return while the job keeps
    running. The packaged GUI/API use the in-process executor instead.
    """
    validate_job_id(job_id)
    if getattr(sys, "frozen", False):
        raise PipelineError(
            errors.CONFIGURATION_ERROR,
            "--no-wait requires the Python source installation.",
            technical_details="The packaged application runs jobs inside the app.",
        )
    command = [sys.executable, "-m", "clipper.viral.worker", job_id]
    kwargs: Dict[str, Any] = {
        "cwd": str(config.project_root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise PipelineError(
            errors.CONFIGURATION_ERROR,
            "The background worker could not be started.",
            technical_details=str(exc),
        ) from exc
