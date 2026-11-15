"""Small standard-library HTTP helpers (JSON and multipart uploads)."""

from __future__ import annotations

import json
import mimetypes
import socket
import ssl
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

from clipper.viral.errors import PipelineError, redact


class HttpRequestError(Exception):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {redact(body)[:500]}")
        self.status = status
        self.body = redact(body)
        self.url = url


def _decode_response(raw: bytes) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Provider returned non-JSON content: {redact(text)[:300]}") from exc
    if not isinstance(data, dict):
        raise ValueError("Provider returned JSON that is not an object.")
    return data


def _request(req: urlrequest.Request, timeout: int) -> bytes:
    context = ssl.create_default_context()
    try:
        with urlrequest.urlopen(req, timeout=timeout, context=context) as response:
            return response.read()
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HttpRequestError(exc.code, body, req.full_url) from exc
    except (urlerror.URLError, socket.timeout, TimeoutError) as exc:
        raise PipelineError(
            "TIMEOUT" if "timed out" in str(exc).lower() else "NETWORK_ERROR",
            "The provider could not be reached.",
            technical_details=str(exc),
            retryable=True,
        ) from exc


def post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Optional[Mapping[str, str]] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json", "Accept": "application/json"}
    merged.update(dict(headers or {}))
    req = urlrequest.Request(url, data=body, headers=merged, method="POST")
    return _decode_response(_request(req, timeout))


def _encode_multipart(
    fields: Mapping[str, str],
    files: Sequence[Tuple[str, Path, str]],
) -> Tuple[bytes, str]:
    boundary = "----ViralClip" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for field_name, path, content_type in files:
        filename = path.name
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post_multipart(
    url: str,
    fields: Mapping[str, str],
    files: Sequence[Tuple[str, Path, str]],
    headers: Optional[Mapping[str, str]] = None,
    timeout: int = 300,
) -> Dict[str, Any]:
    body, content_type = _encode_multipart(fields, files)
    merged = {"Content-Type": content_type, "Accept": "application/json"}
    merged.update(dict(headers or {}))
    req = urlrequest.Request(url, data=body, headers=merged, method="POST")
    return _decode_response(_request(req, timeout))


def guess_content_type(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"
