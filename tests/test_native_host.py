"""The native-messaging host: framing, relaying, and real round trips.

The end-to-end tests start an ipc.Server under the temporary DOWNPOUR_HOME,
then run the host as a separate process exactly as a browser would and feed
it framed messages on standard input.
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
from multiprocessing import AuthenticationError
from pathlib import Path

import pytest

from downpour.browser import native_host
from downpour.browser.ids import CHROMIUM_EXTENSION_IDS, FIREFOX_EXTENSION_ID, HOST_NAME
from downpour.core import ipc

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = f"chrome-extension://{CHROMIUM_EXTENSION_IDS[0]}/"
FIREFOX_MANIFEST = rf"C:\Users\someone\AppData\Local\Downpour\{HOST_NAME}.firefox.json"


def frame(message) -> bytes:
    payload = message if isinstance(message, bytes) else json.dumps(message).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def payloads(data: bytes) -> list[bytes]:
    """Split stdout into frames; anything that is not a whole frame fails."""
    out = []
    while data:
        assert len(data) >= 4, f"stray bytes on stdout: {data!r}"
        (length,) = struct.unpack("<I", data[:4])
        payload, data = data[4 : 4 + length], data[4 + length :]
        assert len(payload) == length, "truncated reply"
        out.append(payload)
    return out


def unframe(data: bytes) -> list[dict]:
    return [json.loads(payload.decode("utf-8")) for payload in payloads(data)]


def serve_bytes(data: bytes) -> tuple[int, list[dict]]:
    out = io.BytesIO()
    code = native_host.serve(io.BytesIO(data), out)
    return code, unframe(out.getvalue())


@pytest.fixture
def echo(monkeypatch):
    monkeypatch.setattr(native_host, "handle", lambda msg: {"ok": True, "echo": msg})


@pytest.fixture
def fake_ipc(monkeypatch):
    """Record which ipc call the host makes; launching is never allowed."""
    calls = []
    replies = {}

    def request(msg, timeout=15.0):
        calls.append(("request", msg, timeout))
        return reply_for(msg)

    def request_or_launch(msg, launch_args=("--tray",), wait=30.0):
        calls.append(("request_or_launch", msg, wait))
        return reply_for(msg)

    def reply_for(msg):
        reply = replies.get(msg["type"], {"ok": True})
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def launch_app(*args):
        raise AssertionError("the host tried to start the app directly")

    monkeypatch.setattr(ipc, "request", request)
    monkeypatch.setattr(ipc, "request_or_launch", request_or_launch)
    monkeypatch.setattr(ipc, "launch_app", launch_app)
    return calls, replies


# -- framing -----------------------------------------------------------------


def test_round_trip_keeps_unicode_and_writes_ascii(echo):
    message = {"type": "add", "items": [{"url": "https://example.com/f.zip", "text": "naïve ✓ 文件"}]}
    out = io.BytesIO()
    assert native_host.serve(io.BytesIO(frame(message) + frame({"type": "show"})), out) == 0
    raw = out.getvalue()
    assert all(payload.isascii() for payload in payloads(raw))
    assert unframe(raw) == [{"ok": True, "echo": message}, {"ok": True, "echo": {"type": "show"}}]


def test_clean_end_of_input_exits_quietly(echo):
    assert serve_bytes(b"") == (0, [])


@pytest.mark.parametrize("data", [b"\x05\x00", frame({"type": "hello"})[:-3]])
def test_truncated_input_is_an_error(echo, data):
    assert serve_bytes(data) == (1, [])


def test_bad_json_is_answered_and_the_stream_continues(echo):
    code, replies = serve_bytes(frame(b"{not json") + frame(b"\xff\xfe") + frame({"type": "show"}))
    assert code == 0
    assert [r["ok"] for r in replies] == [False, False, True]
    assert "JSON" in replies[0]["error"]


def test_oversized_message_is_skipped_without_losing_sync(echo, monkeypatch):
    monkeypatch.setattr(native_host, "MAX_INCOMING", 16)
    code, replies = serve_bytes(frame({"type": "add", "items": ["x" * 100]}) + frame({"type": "show"}))
    assert code == 0
    assert replies[0]["ok"] is False and "too large" in replies[0]["error"]
    assert replies[1] == {"ok": True, "echo": {"type": "show"}}


def test_oversized_reply_becomes_an_error(monkeypatch):
    monkeypatch.setattr(native_host, "MAX_REPLY", 64)
    monkeypatch.setattr(native_host, "handle", lambda msg: {"ok": True, "junk": "x" * 1000})
    code, replies = serve_bytes(frame({"type": "hello"}))
    assert code == 0
    assert replies == [{"ok": False, "error": "Downpour's reply was too large for the browser"}]


def test_browser_closing_its_end_ends_the_host(echo):
    class Closed(io.RawIOBase):
        def writable(self):
            return True

        def write(self, data):
            raise BrokenPipeError

    assert native_host.serve(io.BytesIO(frame({"type": "show"})), Closed()) == 0


# -- relaying -------------------------------------------------------------------


def test_hello_never_starts_the_app(fake_ipc):
    calls, replies = fake_ipc
    replies["hello"] = ipc.NotRunning("Downpour has never run on this account")
    reply = native_host.handle({"type": "hello"})
    assert reply["ok"] is False and reply["running"] is False and reply["error"]
    assert [c[0] for c in calls] == ["request"]
    assert calls[0][2] == native_host.HELLO_TIMEOUT


def test_hello_passes_the_app_reply_through(fake_ipc):
    calls, replies = fake_ipc
    replies["hello"] = {"ok": True, "app": "Downpour", "version": "0.1.0", "capture": True}
    assert native_host.handle({"type": "hello"}) == replies["hello"]


@pytest.mark.parametrize(
    ("kind", "wait"),
    [("offer", native_host.OFFER_LAUNCH_WAIT), ("add", native_host.LAUNCH_WAIT),
     ("media", native_host.LAUNCH_WAIT), ("show", native_host.LAUNCH_WAIT)],
)
def test_other_messages_start_the_app_when_needed(fake_ipc, kind, wait):
    calls, replies = fake_ipc
    replies[kind] = {"ok": True, "accept": True}
    msg = {"type": kind, "download": {"url": "https://example.com/a.zip"}}
    assert native_host.handle(msg) == {"ok": True, "accept": True}
    assert calls == [("request_or_launch", msg, wait)]


def test_offer_wait_leaves_room_inside_the_extension_timeout():
    # The extension resumes the browser download after 25 s; the host must
    # have answered by then: start-up wait plus ipc.request's 15 s.
    assert native_host.OFFER_LAUNCH_WAIT + 15.0 < 25.0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ipc.NotRunning("Downpour did not start in time"), "did not start"),
        (TimeoutError(), "did not answer"),
        (AuthenticationError("digest received was wrong"), "refused"),
        (EOFError(), "closed the connection"),
        (BrokenPipeError(), "closed the connection"),
        (RuntimeError("boom"), "boom"),
    ],
)
def test_failures_become_replies(fake_ipc, error, expected):
    calls, replies = fake_ipc
    replies["offer"] = error
    reply = native_host.handle({"type": "offer", "download": {"url": "https://example.com/a.zip"}})
    assert reply["ok"] is False
    assert expected in reply["error"]


@pytest.mark.parametrize("msg", [[1, 2], "hello", None, {"type": "format-c"}, {"no": "type"}])
def test_malformed_messages_never_reach_the_app(fake_ipc, msg):
    calls, _ = fake_ipc
    reply = native_host.handle(msg)
    assert reply["ok"] is False and reply["error"]
    assert calls == []


# -- who started us -------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [ORIGIN, "--parent-window=0"],  # Chrome, Edge, Brave on Windows
        [ORIGIN.upper()],
        [FIREFOX_MANIFEST, FIREFOX_EXTENSION_ID],  # Firefox
        [FIREFOX_MANIFEST],
        [FIREFOX_EXTENSION_ID],
    ],
)
def test_browser_invocations_are_recognised(args):
    assert native_host.is_native_host_invocation(["Downpour.exe", *args])


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--tray"],
        ["https://example.com/file.zip"],
        ["add", "https://example.com/file.zip"],
        [r"C:\Users\someone\settings.json", "someone@example.com"],
    ],
)
def test_ordinary_command_lines_are_not_mistaken_for_a_browser(args):
    assert not native_host.is_native_host_invocation(["Downpour.exe", *args])


def test_host_does_not_import_wx():
    code = "import sys, downpour.browser.native_host; sys.exit('wx' in sys.modules)"
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")


# -- end to end ----------------------------------------------------------------


def run_host(home: Path, messages: list, argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, DOWNPOUR_HOME=str(home), PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, *argv],
        input=b"".join(frame(m) for m in messages),
        capture_output=True,
        cwd=str(home),  # browsers start a host in its own folder, not the source tree
        env=env,
        timeout=60,
    )


@pytest.fixture
def running_app(isolated_home):
    """A stand-in for the app: answers on the real pipe and records requests."""
    received = []

    def handler(msg):
        received.append(msg)
        if msg["type"] == "hello":
            return {"ok": True, "app": "Downpour", "version": "9.8.7", "capture": True}
        if msg["type"] == "offer":
            return {"ok": True, "accept": msg["download"]["url"].endswith(".zip")}
        return {"ok": True}

    server = ipc.Server(handler)
    server.start()
    try:
        yield received
    finally:
        server.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["-m", "downpour.browser.native_host", ORIGIN, "--parent-window=0"],
        ["-m", "downpour", FIREFOX_MANIFEST, FIREFOX_EXTENSION_ID],  # the app's own entry point
    ],
    ids=["chromium-module", "firefox-app-entry"],
)
def test_messages_reach_the_running_app_and_back(running_app, isolated_home, argv):
    secret = "s3cret-cookie-value"
    offer_zip = {
        "type": "offer",
        "download": {
            "url": "https://files.example.com/big.zip",
            "filename": "größe ✓.zip",
            "size": 123456789,
            "cookies": [{"name": "sid", "value": secret, "domain": "files.example.com", "path": "/"}],
        },
    }
    offer_page = {"type": "offer", "download": {"url": "https://files.example.com/page.html"}}
    add = {"type": "add", "items": [{"url": "https://files.example.com/a.iso", "text": "Disc 1"}], "pick": True}
    messages = [{"type": "hello"}, offer_zip, offer_page, add, {"type": "show"}]

    proc = run_host(isolated_home, messages, argv)

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert unframe(proc.stdout) == [
        {"ok": True, "app": "Downpour", "version": "9.8.7", "capture": True},
        {"ok": True, "accept": True},
        {"ok": True, "accept": False},
        {"ok": True},
        {"ok": True},
    ]
    assert running_app == messages  # relayed unchanged, Unicode and all
    log = (isolated_home / native_host.LOG_NAME).read_text(encoding="utf-8")
    assert "offer: accepted" in log and "offer: declined" in log
    assert "example.com" not in log and secret not in log  # no browsing data in the log


def test_hello_when_downpour_has_never_run(isolated_home):
    proc = run_host(isolated_home, [{"type": "hello"}], ["-m", "downpour.browser.native_host", ORIGIN])
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    [reply] = unframe(proc.stdout)
    assert reply["ok"] is False and reply["running"] is False


def test_hello_when_downpour_is_not_running(isolated_home):
    ipc.load_key(create=True)  # it has run before, but nothing owns the pipe now
    messages = [{"type": "hello"}, {"type": "hello"}]
    proc = run_host(isolated_home, messages, ["-m", "downpour.browser.native_host", ORIGIN])
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert [(r["ok"], r["running"]) for r in unframe(proc.stdout)] == [(False, False), (False, False)]
