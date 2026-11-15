import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from clipper.viral.config import ViralConfig
from clipper.viral.packaging import write_json
from clipper.viral.server import ViralAPIHandler, make_server


class _AbortedWriter:
    def write(self, chunk):
        raise ConnectionResetError(10054, "connection reset")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ViralConfig(
            project_root=self.root,
            output_dir=self.root / "output",
            temp_dir=self.root / "tmp",
            cache_dir=self.root / "cache",
            api_token="secret-token",
        )
        self.job_id = uuid.uuid4().hex
        job_root = self.config.output_dir / self.job_id
        (job_root / "metadata").mkdir(parents=True)
        (job_root / "clips").mkdir(parents=True)
        self.clip_path = job_root / "clips" / "clip_001.mp4"
        self.clip_path.write_bytes(b"0123456789")
        self.result_path = write_json(job_root / "result.json", {"job_id": self.job_id, "clips": []})
        self.report_path = job_root / "report.md"
        self.report_path.write_bytes(b"# Report\n")
        write_json(
            job_root / "metadata" / "job.json",
            {
                "job_id": self.job_id,
                "source_url": "https://example.com/v",
                "status": "completed",
                "progress": 100,
                "current_step": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "output_path": str(job_root),
                "result_path": str(self.result_path),
                "report_path": str(self.report_path),
                "package_path": str(job_root / "package.zip"),
            },
        )
        self.server = make_server(self.config, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.manager.shutdown(wait=False)
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response, payload
        finally:
            connection.close()

    def test_server_uses_an_ephemeral_port_when_requested(self):
        self.assertGreater(self.port, 0)
        self.assertNotEqual(self.port, self.config.api_port)
        response, payload = self._request("GET", "/health?token=secret-token")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["status"], "ok")

    def test_health_requires_the_configured_token(self):
        response, _ = self._request("GET", "/health")
        self.assertEqual(response.status, 401)
        response, payload = self._request(
            "GET", "/health", headers={"Authorization": "Bearer secret-token"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["status"], "ok")

    def test_token_can_be_supplied_as_a_query_parameter(self):
        response, payload = self._request("GET", "/health?token=secret-token")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["status"], "ok")

    def test_job_result_and_file_endpoints(self):
        headers = {"Authorization": "Bearer secret-token"}
        response, payload = self._request("GET", f"/api/jobs/{self.job_id}", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["job_id"], self.job_id)

        response, payload = self._request(
            "GET", f"/api/jobs/{self.job_id}/results", headers=headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["job_id"], self.job_id)

        response, payload = self._request(
            "GET", f"/files/{self.job_id}/clips/clip_001.mp4", headers=headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, b"0123456789")

        response, payload = self._request(
            "GET",
            f"/files/{self.job_id}/clips/clip_001.mp4",
            headers={**headers, "Range": "bytes=2-5"},
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(payload, b"2345")
        self.assertIn("bytes 2-5/10", response.getheader("Content-Range", ""))

    def test_head_file_response_has_no_body(self):
        response, payload = self._request(
            "HEAD",
            f"/files/{self.job_id}/clips/clip_001.mp4",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, b"")
        self.assertEqual(response.getheader("Content-Length"), "10")

    def test_path_traversal_is_rejected(self):
        response, payload = self._request(
            "GET",
            f"/files/{self.job_id}/../secret.txt",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "UNSAFE_PATH")

    def test_static_ui_loads_without_a_token(self):
        response, payload = self._request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type", ""))
        self.assertIn(b"/app.js", payload)

        response, payload = self._request("GET", "/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.getheader("Content-Type", ""))
        self.assertTrue(payload)

        response, _ = self._request("GET", "/styles.css")
        self.assertEqual(response.status, 200)

    def test_ui_shell_exposes_the_expected_controls(self):
        response, payload = self._request("GET", "/")
        self.assertEqual(response.status, 200)
        html = payload.decode("utf-8")
        for element_id in (
            "job-form",
            "job-list",
            "tab-clips",
            "tab-logs",
            "tab-report",
            "panel-report",
            "token-dialog",
            "delete-dialog",
            "quality",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/app.js", html)
        self.assertIn("/styles.css", html)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy", ""))

    def test_static_ui_does_not_weaken_the_api_token(self):
        response, _ = self._request("GET", "/api/jobs")
        self.assertEqual(response.status, 401)

    def test_job_log_endpoint_returns_the_requested_tail(self):
        log_dir = Path(self.config.output_dir) / self.job_id / "logs"
        log_dir.mkdir(parents=True)
        records = [
            {"stage": "transcription_started", "level": "INFO"},
            {"stage": "analysis_started", "level": "INFO"},
            {"stage": "clip_ready", "level": "INFO"},
        ]
        (log_dir / "job.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        headers = {"Authorization": "Bearer secret-token"}
        response, payload = self._request(
            "GET", f"/api/jobs/{self.job_id}/log?tail=2", headers=headers
        )
        self.assertEqual(response.status, 200)
        lines = json.loads(payload)["lines"]
        self.assertEqual([line["stage"] for line in lines], ["analysis_started", "clip_ready"])

    def test_job_log_endpoint_handles_a_missing_log(self):
        headers = {"Authorization": "Bearer secret-token"}
        response, payload = self._request(
            "GET", f"/api/jobs/{self.job_id}/log", headers=headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["lines"], [])

    def test_report_can_be_served_inline(self):
        headers = {"Authorization": "Bearer secret-token"}
        response, payload = self._request(
            "GET", f"/api/jobs/{self.job_id}/report?inline=1", headers=headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, b"# Report\n")
        self.assertIsNone(response.getheader("Content-Disposition"))

    def test_aborted_file_stream_is_treated_as_a_disconnect(self):
        handler = object.__new__(ViralAPIHandler)
        handler.command = "GET"
        handler.headers = {}
        handler.wfile = _AbortedWriter()
        handler.close_connection = False
        handler.send_response = lambda status: None
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None
        ViralAPIHandler._send_file(handler, self.clip_path, "video/mp4")
        self.assertTrue(handler.close_connection)

    def test_dropped_connection_errors_are_silent(self):
        buffer = io.StringIO()
        try:
            raise ConnectionResetError(10054, "connection reset")
        except ConnectionError:
            with contextlib.redirect_stderr(buffer):
                self.server.handle_error(None, ("127.0.0.1", 12345))
        self.assertEqual(buffer.getvalue(), "")

    def test_unexpected_handler_errors_are_still_logged(self):
        buffer = io.StringIO()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            with contextlib.redirect_stderr(buffer):
                self.server.handle_error(None, ("127.0.0.1", 12345))
        self.assertIn("Exception occurred", buffer.getvalue())

    def test_invalid_post_is_rejected_without_queueing(self):
        response, payload = self._request(
            "POST",
            "/api/jobs",
            headers={
                "Authorization": "Bearer secret-token",
                "Content-Type": "application/json",
            },
            body=json.dumps({"url": ""}),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "INVALID_URL")

    def test_delete_requires_a_token(self):
        response, payload = self._request("DELETE", f"/api/jobs/{self.job_id}")
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(payload)["error"]["code"], "UNAUTHORIZED")
        self.assertTrue((Path(self.config.output_dir) / self.job_id).exists())

    def test_delete_removes_the_job_and_its_files(self):
        headers = {"Authorization": "Bearer secret-token"}
        response, payload = self._request(
            "DELETE", f"/api/jobs/{self.job_id}", headers=headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload), {"job_id": self.job_id, "deleted": True})
        self.assertFalse((Path(self.config.output_dir) / self.job_id).exists())

        response, payload = self._request(
            "GET", f"/api/jobs/{self.job_id}", headers=headers
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(payload)["error"]["code"], "JOB_NOT_FOUND")

    def test_delete_unknown_job_returns_not_found(self):
        response, payload = self._request(
            "DELETE",
            f"/api/jobs/{uuid.uuid4().hex}",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(payload)["error"]["code"], "JOB_NOT_FOUND")

    def test_delete_rejects_an_active_job(self):
        record_path = Path(self.config.output_dir) / self.job_id / "metadata" / "job.json"
        data = json.loads(record_path.read_text(encoding="utf-8"))
        data["status"] = "downloading"
        data["current_step"] = "downloading"
        record_path.write_text(json.dumps(data), encoding="utf-8")

        response, payload = self._request(
            "DELETE",
            f"/api/jobs/{self.job_id}",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(payload)["error"]["code"], "JOB_NOT_DELETABLE")
        self.assertTrue(record_path.exists())


if __name__ == "__main__":
    unittest.main()
