"""Optional local web UI and JSON API (standard library only)."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.jobs import JobManager
from clipper.viral.validation import ensure_within


WEB_ASSETS: Dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

_CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
)


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _web_dir() -> Path:
    """Resolve the bundled UI directory in source and frozen builds."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "web"
    return Path(__file__).resolve().parent / "web"


def _status_for(exc: PipelineError) -> int:
    if exc.code in (
        errors.INVALID_URL,
        errors.UNSUPPORTED_URL,
        errors.INVALID_OPTIONS,
        errors.UNSAFE_PATH,
    ):
        return HTTPStatus.BAD_REQUEST
    if exc.code in (errors.JOB_NOT_FOUND,):
        return HTTPStatus.NOT_FOUND
    if exc.code in (errors.JOB_NOT_RETRYABLE, errors.JOB_NOT_DELETABLE):
        return HTTPStatus.CONFLICT
    if exc.code in (errors.CONFIGURATION_ERROR,):
        return HTTPStatus.SERVICE_UNAVAILABLE
    return HTTPStatus.INTERNAL_SERVER_ERROR


class ViralAPIHandler(BaseHTTPRequestHandler):
    server_version = "ClippingStudioViral/1.0"
    protocol_version = "HTTP/1.1"
    config: ViralConfig
    manager: JobManager

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[api] {self.address_string()} {fmt % args}", file=sys.stderr)

    def _authorized(self) -> bool:
        if not self.config.api_token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] == self.config.api_token:
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("token", [""])[0] == self.config.api_token

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, data: Any) -> None:
        self._send(status, _json_bytes(data), "application/json; charset=utf-8")

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _serve_static(self, route: str) -> None:
        filename, content_type = WEB_ASSETS[route]
        path = _web_dir() / filename
        try:
            body = path.read_bytes()
        except OSError:
            return self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "UI asset is missing.")
        return self._send(
            HTTPStatus.OK,
            body,
            content_type,
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": _CSP,
            },
        )

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 1_048_576:
            raise PipelineError(errors.INVALID_OPTIONS, "Request body is missing or too large.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError(errors.INVALID_OPTIONS, "Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise PipelineError(errors.INVALID_OPTIONS, "Request body must be a JSON object.")
        return data

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in WEB_ASSETS:
            return self._serve_static(parsed.path)
        if not self._authorized():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "A valid API token is required.")
            return
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            if parsed.path == "/health":
                return self._send_json(HTTPStatus.OK, {"status": "ok"})
            if parts[:2] == ["api", "jobs"]:
                if len(parts) == 2:
                    return self._send_json(HTTPStatus.OK, {"jobs": [r.to_dict() for r in self.manager.list_jobs()]})
                job_id = parts[2]
                if len(parts) == 3:
                    return self._send_json(HTTPStatus.OK, self.manager.get_job(job_id).to_dict())
                if len(parts) == 4 and parts[3] == "results":
                    record = self.manager.get_job(job_id)
                    if record.status != "completed" or not record.result_path:
                        return self._send_error_json(HTTPStatus.CONFLICT, "JOB_NOT_COMPLETED", "The job has not completed.")
                    result_path = Path(record.result_path)
                    if not result_path.exists():
                        return self._send_error_json(HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND", "Result file not found.")
                    return self._send(HTTPStatus.OK, result_path.read_bytes(), "application/json; charset=utf-8")
                if len(parts) == 4 and parts[3] in ("download", "report"):
                    return self._serve_artifact(job_id, parts[3])
                if len(parts) == 4 and parts[3] == "log":
                    return self._serve_job_log(job_id)
            if parts[:1] == ["files"] and len(parts) >= 3:
                return self._serve_job_file(parts[1], "/".join(parts[2:]))
            return self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Unknown endpoint.")
        except ConnectionError:
            # The browser aborted a video/artifact stream; there is no one
            # left to receive an error response.
            self.close_connection = True
        except PipelineError as exc:
            self._send_error_json(_status_for(exc), exc.code, exc.message)
        except Exception as exc:  # keep API failures JSON-shaped
            self.log_message("unhandled GET error: %s", exc)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Unexpected server error.")

    def _serve_artifact(self, job_id: str, kind: str) -> None:
        record = self.manager.get_job(job_id)
        if kind == "download":
            path = Path(record.package_path) if record.package_path else None
            filename = "viral_clips.zip"
            content_type = "application/zip"
        else:
            path = Path(record.report_path) if record.report_path else None
            filename = "report.md"
            content_type = "text/markdown; charset=utf-8"
        if path is None or not path.exists():
            return self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Artifact is not ready.")
        ensure_within(Path(record.output_path), path)
        query = parse_qs(urlparse(self.path).query)
        inline = kind == "report" and query.get("inline", ["0"])[0].lower() in (
            "1",
            "true",
            "yes",
        )
        return self._send_file(
            path, content_type, attachment_name=None if inline else filename
        )

    def _serve_job_log(self, job_id: str) -> None:
        record = self.manager.get_job(job_id)
        if not record.output_path:
            return self._send_json(HTTPStatus.OK, {"job_id": job_id, "lines": []})
        job_root = Path(record.output_path)
        log_path = ensure_within(job_root, job_root / "logs" / "job.jsonl")
        if not log_path.exists() or not log_path.is_file():
            return self._send_json(HTTPStatus.OK, {"job_id": job_id, "lines": []})
        query = parse_qs(urlparse(self.path).query)
        try:
            tail = int(query.get("tail", ["200"])[0])
        except (TypeError, ValueError):
            tail = 200
        tail = max(1, min(1000, tail))
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self._send_json(HTTPStatus.OK, {"job_id": job_id, "lines": []})
        lines = []
        for line in text.splitlines()[-tail:]:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = {"level": "INFO", "stage": "log", "message": line}
            if not isinstance(value, dict):
                value = {"level": "INFO", "stage": "log", "message": line}
            lines.append(value)
        return self._send_json(HTTPStatus.OK, {"job_id": job_id, "lines": lines})

    def _serve_job_file(self, job_id: str, relative: str) -> None:
        record = self.manager.get_job(job_id)
        job_root = Path(record.output_path)
        candidate = ensure_within(job_root, job_root / relative)
        if not candidate.exists() or not candidate.is_file():
            return self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "File not found.")
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        return self._send_file(candidate, content_type)

    def _send_file(self, path: Path, content_type: str, attachment_name: Optional[str] = None) -> None:
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match:
                if match.group(1):
                    start = int(match.group(1))
                if match.group(2):
                    end = min(size - 1, int(match.group(2)))
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if attachment_name:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except ConnectionError:
            # Browsers abort media streams on seek, reload, and tab close.
            # There is no client left to receive an error response.
            self.close_connection = True

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "A valid API token is required.")
            return
        parts = [unquote(part) for part in urlparse(self.path).path.split("/") if part]
        try:
            if parts == ["api", "jobs"]:
                body = self._read_json_body()
                url = body.get("url")
                options = body.get("options") or {}
                record = self.manager.create_job(url, options)
                return self._send_json(HTTPStatus.ACCEPTED, {"job_id": record.job_id, "status": record.status})
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "retry":
                record = self.manager.retry_job(parts[2])
                return self._send_json(HTTPStatus.ACCEPTED, {"job_id": record.job_id, "status": record.status})
            return self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Unknown endpoint.")
        except ConnectionError:
            self.close_connection = True
        except PipelineError as exc:
            self._send_error_json(_status_for(exc), exc.code, exc.message)
        except Exception as exc:
            self.log_message("unhandled POST error: %s", exc)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Unexpected server error.")

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "A valid API token is required.")
            return
        parts = [unquote(part) for part in urlparse(self.path).path.split("/") if part]
        try:
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                self.manager.delete_job(parts[2])
                return self._send_json(HTTPStatus.OK, {"job_id": parts[2], "deleted": True})
            return self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Unknown endpoint.")
        except ConnectionError:
            self.close_connection = True
        except PipelineError as exc:
            self._send_error_json(_status_for(exc), exc.code, exc.message)
        except Exception as exc:
            self.log_message("unhandled DELETE error: %s", exc)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Unexpected server error.")

    def do_HEAD(self) -> None:
        return self.do_GET()


class ViralHTTPServer(ThreadingHTTPServer):
    """Threaded server that treats dropped clients as normal disconnects."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def make_server(config: ViralConfig, host: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer:
    manager = JobManager(config)
    handler = type(
        "BoundViralAPIHandler",
        (ViralAPIHandler,),
        {"config": config, "manager": manager},
    )
    server = ViralHTTPServer(
        (host or config.api_host, config.api_port if port is None else port), handler
    )
    server.daemon_threads = True
    server.manager = manager  # type: ignore[attr-defined]
    return server


def run_server(config: ViralConfig, host: Optional[str] = None, port: Optional[int] = None) -> None:
    server = make_server(config, host, port)
    actual_host, actual_port = server.server_address[:2]
    print(f"Viral Clips API/UI: http://{actual_host}:{actual_port}/", file=sys.stderr)
    if config.api_token:
        print("API token is enabled; append ?token=... to the UI URL.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server.manager.shutdown(wait=False)  # type: ignore[attr-defined]



