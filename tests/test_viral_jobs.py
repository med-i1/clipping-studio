import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.jobs import JobManager, _pid_alive, spawn_detached_worker
from clipper.viral.worker import main as worker_main
from clipper.viral.models import PipelineResult
from clipper.viral.packaging import write_json


class FakePipeline:
    def __init__(self, config: ViralConfig, fail_until: int = 0):
        self.config = config
        self.fail_until = fail_until
        self.calls = 0

    def run(
        self,
        job_id,
        source_url,
        options,
        created_at=None,
        progress_callback=None,
        logger=None,
        job_root=None,
    ):
        self.calls += 1
        if self.calls <= self.fail_until:
            raise PipelineError(
                errors.DOWNLOAD_FAILED,
                "Temporary download failure.",
                technical_details="simulated",
                retryable=True,
            )
        root = Path(job_root) if job_root is not None else self.config.output_dir / job_id
        result_path = write_json(root / "result.json", {"job_id": job_id, "status": "completed"})
        report_path = root / "report.md"
        report_path.write_text("# Report\n", encoding="utf-8")
        package_path = root / "package.zip"
        package_path.write_bytes(b"zip")
        if progress_callback:
            progress_callback(100, "completed", "Done")
        return PipelineResult(
            result={"job_id": job_id},
            output_dir=root,
            result_path=result_path,
            report_path=report_path,
            package_path=package_path,
        )


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ViralConfig(
            project_root=self.root,
            output_dir=self.root / "output",
            temp_dir=self.root / "tmp",
            cache_dir=self.root / "cache",
            job_workers=1,
        )
        self.manager = None
        self.counter = {"value": 0}

    def tearDown(self):
        if self.manager is not None:
            self.manager.shutdown(wait=True)
        self.temp.cleanup()

    def _factory(self, fail_first=0):
        state = {"remaining": max(0, int(fail_first))}

        def build(config, logger=None):
            self.counter["value"] += 1
            fail_now = state["remaining"] > 0
            state["remaining"] = max(0, state["remaining"] - 1)
            return FakePipeline(config, fail_until=1 if fail_now else 0)

        return build

    def test_create_job_completes_and_persists_artifacts(self):
        self.manager = JobManager(self.config)
        with patch("clipper.viral.jobs.build_pipeline", side_effect=self._factory()):
            record = self.manager.create_job(
                "https://example.com/watch?v=abc123",
                {"number_of_clips": 2, "language": "ar"},
            )
            self.assertEqual(record.status, "queued")
            self.assertEqual(record.options["number_of_clips"], 2)
            finished = self.manager.wait(record.job_id, timeout=10)

        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.progress, 100.0)
        self.assertEqual(finished.current_step, "completed")
        self.assertIsNone(finished.error)
        for value in (finished.result_path, finished.report_path, finished.package_path):
            self.assertTrue(value)
            self.assertTrue(Path(value).exists())

        stored = json.loads(
            (Path(finished.output_path) / "metadata" / "job.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["job_id"], record.job_id)

        jobs = self.manager.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, record.job_id)

        loaded = self.manager.get_job(record.job_id)
        self.assertEqual(loaded.status, "completed")

    def test_failed_job_can_be_retried(self):
        self.manager = JobManager(self.config)
        with patch(
            "clipper.viral.jobs.build_pipeline", side_effect=self._factory(fail_first=1)
        ):
            record = self.manager.create_job("https://example.com/watch?v=abc123")
            failed = self.manager.wait(record.job_id, timeout=10)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error["code"], errors.DOWNLOAD_FAILED)
            self.assertTrue(failed.error["retryable"])
            self.assertEqual(failed.attempts, 1)

            retried = self.manager.retry_job(record.job_id)
            self.assertEqual(retried.attempts, 2)
            finished = self.manager.wait(record.job_id, timeout=10)

        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.attempts, 2)
        self.assertIsNone(finished.error)
        self.assertEqual(self.counter["value"], 2)

    def test_failed_job_log_records_technical_details(self):
        self.manager = JobManager(self.config)
        with patch(
            "clipper.viral.jobs.build_pipeline", side_effect=self._factory(fail_first=1)
        ):
            record = self.manager.create_job("https://example.com/watch?v=abc123")
            failed = self.manager.wait(record.job_id, timeout=10)
        self.assertEqual(failed.status, "failed")
        lines = (
            Path(failed.output_path) / "logs" / "job.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
        failure = [event for event in events if event.get("stage") == "job_failed"]
        self.assertEqual(len(failure), 1)
        self.assertEqual(failure[0]["code"], errors.DOWNLOAD_FAILED)
        self.assertEqual(failure[0]["technical_details"], "simulated")

    def test_queued_job_can_be_run_by_a_detached_worker(self):
        self.manager = JobManager(self.config)
        with patch("clipper.viral.jobs.build_pipeline", side_effect=self._factory()):
            record = self.manager.create_job(
                "https://example.com/watch?v=abc123", submit=False
            )
            self.assertEqual(record.status, "queued")
            self.assertEqual(self.counter["value"], 0)
            finished = self.manager.execute_job(record.job_id)
        self.assertEqual(finished.status, "completed")
        self.assertEqual(self.counter["value"], 1)

    def test_worker_entry_point_completes_a_queued_job(self):
        self.manager = JobManager(self.config)
        with patch("clipper.viral.jobs.build_pipeline", side_effect=self._factory()):
            record = self.manager.create_job(
                "https://example.com/watch?v=abc123", submit=False
            )
        with patch("clipper.viral.worker.load_config", return_value=self.config), patch(
            "clipper.viral.jobs.build_pipeline", side_effect=self._factory()
        ):
            code = worker_main([record.job_id])
        self.assertEqual(code, 0)
        stored = json.loads(
            (Path(record.output_path) / "metadata" / "job.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["status"], "completed")

    def test_spawn_detached_worker_uses_the_worker_module(self):
        self.manager = JobManager(self.config)
        job_id = uuid.uuid4().hex
        with patch("clipper.viral.jobs.subprocess.Popen") as popen:
            popen.return_value.pid = 4321
            process = spawn_detached_worker(self.config, job_id)
        self.assertEqual(process.pid, 4321)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:4], ["-m", "clipper.viral.worker", job_id])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.root))

    def test_completed_job_cannot_be_retried(self):
        self.manager = JobManager(self.config)
        with patch("clipper.viral.jobs.build_pipeline", side_effect=self._factory()):
            record = self.manager.create_job("https://example.com/watch?v=abc123")
            self.manager.wait(record.job_id, timeout=10)
            with self.assertRaises(PipelineError) as caught:
                self.manager.retry_job(record.job_id)
        self.assertEqual(caught.exception.code, errors.JOB_NOT_RETRYABLE)

    def test_completed_job_can_be_deleted_with_its_files(self):
        self.manager = JobManager(self.config)
        with patch("clipper.viral.jobs.build_pipeline", side_effect=self._factory()):
            record = self.manager.create_job("https://example.com/watch?v=abc123")
            finished = self.manager.wait(record.job_id, timeout=10)
        job_root = Path(finished.output_path)
        self.assertTrue(job_root.exists())

        self.manager.delete_job(record.job_id)

        self.assertFalse(job_root.exists())
        self.assertEqual(self.manager.list_jobs(), [])
        with self.assertRaises(PipelineError) as caught:
            self.manager.get_job(record.job_id)
        self.assertEqual(caught.exception.code, errors.JOB_NOT_FOUND)

    def test_active_job_cannot_be_deleted(self):
        self.manager = JobManager(self.config)
        record = self.manager.create_job(
            "https://example.com/watch?v=abc123", submit=False
        )
        with self.assertRaises(PipelineError) as caught:
            self.manager.delete_job(record.job_id)
        self.assertEqual(caught.exception.code, errors.JOB_NOT_DELETABLE)
        self.assertTrue(Path(record.output_path).exists())

    def test_deleting_a_missing_job_raises_not_found(self):
        self.manager = JobManager(self.config)
        with self.assertRaises(PipelineError) as caught:
            self.manager.delete_job(uuid.uuid4().hex)
        self.assertEqual(caught.exception.code, errors.JOB_NOT_FOUND)

    def test_invalid_url_and_options_are_rejected_before_queueing(self):
        self.manager = JobManager(self.config)
        with self.assertRaises(PipelineError) as caught:
            self.manager.create_job("file:///etc/passwd")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_URL)

        with self.assertRaises(PipelineError) as caught:
            self.manager.create_job(
                "https://example.com/watch?v=abc123",
                {"number_of_clips": 999},
            )
        self.assertEqual(caught.exception.code, errors.INVALID_OPTIONS)
        self.assertEqual(self.manager.list_jobs(), [])

    def test_missing_job_raises_not_found(self):
        self.manager = JobManager(self.config)
        with self.assertRaises(PipelineError) as caught:
            self.manager.get_job(uuid.uuid4().hex)
        self.assertEqual(caught.exception.code, errors.JOB_NOT_FOUND)

    def test_list_jobs_skips_corrupt_records_and_sorts_newest_first(self):
        self.manager = JobManager(self.config)
        base = self.config.output_dir
        older_id = uuid.uuid4().hex
        newer_id = uuid.uuid4().hex
        for job_id, created in ((older_id, "2026-01-01T00:00:00+00:00"), (newer_id, "2026-02-01T00:00:00+00:00")):
            write_json(
                base / job_id / "metadata" / "job.json",
                {
                    "job_id": job_id,
                    "source_url": "https://example.com/v",
                    "status": "completed",
                    "created_at": created,
                    "updated_at": created,
                    "output_path": str(base / job_id),
                },
            )
        (base / "broken" / "metadata").mkdir(parents=True)
        (base / "broken" / "metadata" / "job.json").write_text("{not json", encoding="utf-8")

        jobs = self.manager.list_jobs()
        self.assertEqual([job.job_id for job in jobs], [newer_id, older_id])

    # -- crash recovery -------------------------------------------------

    def _write_active_record(self, worker_pid=None, status="downloading"):
        job_id = uuid.uuid4().hex
        job_root = self.config.output_dir / job_id
        data = {
            "job_id": job_id,
            "source_url": "https://example.com/watch?v=abc123",
            "status": status,
            "progress": 12,
            "current_step": status,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "output_path": str(job_root),
            "options": {},
        }
        if worker_pid is not None:
            data["worker_pid"] = worker_pid
        write_json(job_root / "metadata" / "job.json", data)
        return job_id

    @staticmethod
    def _dead_pid():
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=30)
        return process.pid

    def test_pid_alive_reports_running_and_exited_processes(self):
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(self._dead_pid()))
        self.assertFalse(_pid_alive(None))
        self.assertFalse(_pid_alive(0))

    def test_job_from_a_dead_process_is_marked_interrupted_on_startup(self):
        job_id = self._write_active_record(worker_pid=self._dead_pid())

        self.manager = JobManager(self.config)
        record = self.manager.get_job(job_id)

        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error["code"], errors.JOB_INTERRUPTED)
        self.assertTrue(record.error["retryable"])
        log = (self.config.output_dir / job_id / "logs" / "job.jsonl").read_text(encoding="utf-8")
        self.assertIn("job_interrupted", log)

    def test_legacy_active_record_without_a_pid_is_recovered(self):
        job_id = self._write_active_record(worker_pid=None, status="analyzing")
        self.manager = JobManager(self.config)
        self.assertEqual(self.manager.get_job(job_id).status, "failed")

    def test_interrupted_job_can_be_retried(self):
        job_id = self._write_active_record(worker_pid=self._dead_pid())
        self.manager = JobManager(self.config)
        with patch("clipper.viral.jobs.build_pipeline", side_effect=self._factory()):
            retried = self.manager.retry_job(job_id)
            self.assertEqual(retried.attempts, 2)
            finished = self.manager.wait(job_id, timeout=10)
        self.assertEqual(finished.status, "completed")

    def test_interrupted_job_can_be_deleted(self):
        job_id = self._write_active_record(worker_pid=self._dead_pid())
        self.manager = JobManager(self.config)
        self.manager.delete_job(job_id)
        self.assertFalse((self.config.output_dir / job_id).exists())

    def test_crash_after_startup_is_recovered_on_retry(self):
        self.manager = JobManager(self.config)
        job_id = self._write_active_record(worker_pid=self._dead_pid())
        with patch("clipper.viral.jobs.build_pipeline", side_effect=self._factory()):
            self.manager.retry_job(job_id)
            finished = self.manager.wait(job_id, timeout=10)
        self.assertEqual(finished.status, "completed")

    def test_job_owned_by_a_live_process_is_left_running(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            job_id = self._write_active_record(worker_pid=sleeper.pid)
            self.manager = JobManager(self.config)
            self.assertEqual(self.manager.get_job(job_id).status, "downloading")
            with self.assertRaises(PipelineError) as caught:
                self.manager.retry_job(job_id)
            self.assertEqual(caught.exception.code, errors.JOB_NOT_RETRYABLE)
            with self.assertRaises(PipelineError) as caught:
                self.manager.delete_job(job_id)
            self.assertEqual(caught.exception.code, errors.JOB_NOT_DELETABLE)
        finally:
            sleeper.kill()
            sleeper.wait(timeout=30)

    def test_assign_worker_records_the_detached_worker_pid(self):
        self.manager = JobManager(self.config)
        record = self.manager.create_job("https://example.com/watch?v=abc123", submit=False)
        self.assertEqual(record.worker_pid, os.getpid())
        self.manager.assign_worker(record.job_id, 4321)
        stored = json.loads(
            (Path(record.output_path) / "metadata" / "job.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["worker_pid"], 4321)


if __name__ == "__main__":
    unittest.main()
