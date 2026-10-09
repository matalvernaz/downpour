"""Test helpers: a scriptable HTTP server and polling utilities."""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


def payload(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(size)


@dataclass
class FileSpec:
    data: bytes
    ranges: bool = True
    chunked: bool = False
    close_delimited: bool = False
    disposition: str | None = None
    content_type: str = "application/octet-stream"
    delay: float = 0.0  # seconds slept per 16 KB block sent
    fail_first: int = 0  # answer this many requests with 503 first
    status: int | None = None  # answer every request with this status
    max_range: int | None = None  # cap the bytes in one ranged reply
    max_conn: int | None = None  # concurrent requests allowed; the rest get 503
    refuse_status: int = 503
    drop_after: int | None = None  # cut one connection after this many bytes
    redirect_to: str | None = None


@dataclass
class Request:
    path: str
    range: str | None
    headers: dict = field(default_factory=dict)

    @property
    def start(self) -> int | None:
        m = re.match(r"bytes=(\d+)-", self.range or "")
        return int(m.group(1)) if m else None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):  # noqa: N802
        state: Server = self.server.state  # type: ignore[attr-defined]
        path = unquote(urlsplit(self.path).path)
        with state.lock:
            state.requests.append(Request(path, self.headers.get("Range"), dict(self.headers)))
            spec = state.files.get(path)
        if spec is None:
            return self._plain(404)
        if spec.redirect_to:
            self.send_response(302)
            self.send_header("Location", spec.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if spec.status:
            return self._plain(spec.status)
        refuse = None
        with state.lock:
            if spec.fail_first > 0:
                spec.fail_first -= 1
                refuse = 503
            else:
                state.active[path] = state.active.get(path, 0) + 1
                if spec.max_conn and state.active[path] > spec.max_conn:
                    state.active[path] -= 1
                    state.refused += 1
                    refuse = spec.refuse_status
                else:
                    state.peak[path] = max(state.peak.get(path, 0), state.active[path])
        if refuse is not None:
            return self._plain(refuse)
        try:
            self._serve(state, spec)
        except (ConnectionError, OSError):
            pass
        finally:
            with state.lock:
                state.active[path] -= 1

    def _plain(self, status: int):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, state: "Server", spec: FileSpec):
        data = spec.data
        start, end, status = 0, len(data), 200
        rng = self.headers.get("Range")
        if rng and spec.ranges:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            if m.group(2):
                end = min(len(data), int(m.group(2)) + 1)
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if spec.max_range:
                end = min(end, start + spec.max_range)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", spec.content_type)
        if spec.ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end - 1}/{len(data)}")
        if spec.disposition:
            self.send_header("Content-Disposition", spec.disposition)
        if spec.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif spec.close_delimited:
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(end - start))
        self.end_headers()
        sent = 0
        pos = start
        while pos < end:
            block = data[pos : min(end, pos + 16384)]
            with state.lock:
                drop = spec.drop_after is not None and sent + len(block) > spec.drop_after
                if drop:
                    spec.drop_after = None
            if drop:
                self.connection.shutdown(2)
                self.close_connection = True
                return
            if spec.chunked:
                self.wfile.write(f"{len(block):x}\r\n".encode() + block + b"\r\n")
            else:
                self.wfile.write(block)
            sent += len(block)
            pos += len(block)
            with state.lock:
                state.bytes_sent += len(block)
            if spec.delay:
                time.sleep(spec.delay)
        if spec.chunked:
            self.wfile.write(b"0\r\n\r\n")


class Server:
    def __init__(self):
        self.lock = threading.Lock()
        self.files: dict[str, FileSpec] = {}
        self.requests: list[Request] = []
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.refused = 0
        self.bytes_sent = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.state = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def add(self, path: str, data: bytes = b"", **options) -> str:
        self.files[path] = FileSpec(data=data, **options)
        return self.url(path)

    def url(self, path: str) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}{path}"

    def requests_for(self, path: str) -> list[Request]:
        with self.lock:
            return [r for r in self.requests if r.path == path]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_for(engine, download_id: int, statuses, timeout: float = 15.0):
    from downpour.core.models import Status

    if isinstance(statuses, Status):
        statuses = {statuses}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dl = engine.get(download_id)
        if dl is not None and dl.status in statuses and not engine.is_running(download_id):
            return dl
        time.sleep(0.02)
    dl = engine.get(download_id)
    raise AssertionError(f"timed out waiting for {statuses}; status={dl and dl.status} error={dl and dl.error}")


def wait_until(predicate, timeout: float = 10.0, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message}")


def read(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()
