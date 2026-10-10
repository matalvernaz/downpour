"""The native-messaging host that links the browser extension to Downpour.

A WebExtension cannot open a named pipe or start a program. It can only ask
the browser to start a registered "native messaging host" and exchange
messages with it over the host's standard input and output, each one a UTF-8
JSON object behind a 4-byte little-endian length. This module is that host:
it relays every message to the running app over the per-user pipe
(``downpour.core.ipc``) and frames the app's reply back to the browser.

Browsers start a fresh host process for every message, so the host stays
small: no wx, no settings, nothing slow at import time. A browser also cannot
pass extra arguments to a host, which is why the frozen app is a single
executable that checks ``is_native_host_invocation`` at startup and runs
``main`` instead of the GUI.

Standard output belongs to the browser and one stray print would corrupt the
stream, so while the host runs ``sys.stdout`` points at stderr (which
browsers log) and diagnostics go to native-host.log in the logs folder. The
extension's own log arrives as ``log`` messages, which the host writes to
extension.log itself, without involving the app (see core/logs.py).
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
import time
from multiprocessing import AuthenticationError
from pathlib import Path
from typing import BinaryIO, Sequence

from downpour import __version__
from downpour.browser.ids import FIREFOX_EXTENSION_ID, HOST_NAME
from downpour.core import ipc, logs

log = logging.getLogger(__name__)

HEADER = struct.Struct("<I")
MAX_INCOMING = ipc.MAX_MESSAGE  # the pipe refuses anything larger anyway
MAX_REPLY = 1024 * 1024  # browsers disconnect a host that sends more
LOG_NAME = "native-host.log"

MESSAGE_TYPES = frozenset({"hello", "offer", "add", "media", "show", "log"})

# "hello" is the popup's status check: answer fast and never start the app.
HELLO_TIMEOUT = 5.0
# The extension resumes a paused browser download when no answer arrives
# within 25 seconds. ipc.request then allows the app 15 seconds to answer, so
# an offer may spend only about 8 seconds waiting for a cold start; the
# browser keeping the download beats Downpour taking it after the extension
# has given up and resumed it, which would download the file twice.
OFFER_LAUNCH_WAIT = 8.0
LAUNCH_WAIT = 30.0


class ProtocolError(Exception):
    """The input stream broke the framing and cannot be trusted any more."""


class MessageTooLarge(Exception):
    """A message exceeded MAX_INCOMING; it was skipped, the stream is intact."""

    def __init__(self, length: int):
        super().__init__(f"message of {length} bytes is too large")
        self.length = length


def _read_exactly(reader: BinaryIO, size: int) -> bytes:
    """Up to ``size`` bytes; fewer only at end of input. Pipes return short reads."""
    chunks = []
    remaining = size
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(reader: BinaryIO) -> bytes | None:
    """The next message payload, or None at a clean end of input."""
    header = _read_exactly(reader, HEADER.size)
    if not header:
        return None
    if len(header) < HEADER.size:
        raise ProtocolError("input ended inside a message header")
    (length,) = HEADER.unpack(header)
    if length > MAX_INCOMING:
        remaining = length
        while remaining:
            chunk = reader.read(min(remaining, 1 << 16))
            if not chunk:
                raise ProtocolError("input ended inside an oversized message")
            remaining -= len(chunk)
        raise MessageTooLarge(length)
    payload = _read_exactly(reader, length)
    if len(payload) < length:
        raise ProtocolError("input ended inside a message")
    return payload


def encode(reply: dict) -> bytes:
    """Frame one reply. ASCII-escaped JSON is valid UTF-8 whatever it contains."""
    payload = json.dumps(reply, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_REPLY:
        payload = json.dumps(_error("Downpour's reply was too large for the browser")).encode("ascii")
    return HEADER.pack(len(payload)) + payload


def write_message(writer: BinaryIO, reply: dict) -> None:
    data = memoryview(encode(reply))
    while data:
        written = writer.write(data)
        if not written:
            raise BrokenPipeError("could not write to the browser")
        data = data[written:]
    writer.flush()


def _error(text: str, **extra: object) -> dict:
    return {"ok": False, "error": text, **extra}


def handle(msg: object) -> dict:
    """Relay one message to the app and return the reply for the browser.

    Never raises: every failure becomes an ``ok: false`` reply, so the
    extension always hears back and can resume a download it paused.
    """
    if not isinstance(msg, dict):
        return _error("a message must be a JSON object")
    kind = msg.get("type")
    if kind not in MESSAGE_TYPES:
        log.warning("unknown message type %r with keys %s", kind, sorted(msg))
        return _error(f"unknown message type: {kind!r}")
    if kind == "log":
        return _extension_log(msg)
    log.debug("%s: %s", kind, _summary(msg))
    started = time.monotonic()
    try:
        if kind == "hello":
            reply = _hello(msg)
        else:
            wait = OFFER_LAUNCH_WAIT if kind == "offer" else LAUNCH_WAIT
            reply = ipc.request_or_launch(msg, wait=wait)
    except ipc.NotRunning as e:
        reply = _error(str(e) or "Downpour is not running", running=False)
    except TimeoutError:
        reply = _error("Downpour did not answer in time")
    except AuthenticationError:
        reply = _error("Downpour refused the connection")
    except (EOFError, ConnectionError):
        reply = _error("Downpour closed the connection without answering")
    except Exception as e:  # noqa: BLE001 - the browser must always get an answer
        log.exception("%s failed", kind)
        reply = _error(str(e) or type(e).__name__)
    log.info("%s: %s (%.1f s)", kind, _outcome(kind, reply), time.monotonic() - started)
    return reply


def _extension_log(msg: dict) -> dict:
    """Write the extension's log entries to extension.log. Never starts the app."""
    source = "extension " + " ".join(str(msg.get(k, ""))[:40] for k in ("version", "browser") if msg.get(k))
    lines = logs.extension_lines(msg.get("entries"), source.strip())
    try:
        logs.append_extension_log(lines)
    except OSError as e:
        log.warning("could not write the extension's log: %r", e)
        return _error(f"could not write the log: {e}")
    log.debug("log: wrote %d extension entries", len(lines))
    return {"ok": True, "written": len(lines)}


def _summary(msg: dict) -> str:
    """The shape of a message, for the detailed log. Like _outcome it leaves
    out addresses, names and cookie details: the app's own log records the
    downloads themselves, redacted (see core/logs.py)."""

    def item(entry: object) -> str:
        if not isinstance(entry, dict):
            return "not an object"
        size = entry.get("size")
        cookies = entry.get("cookies")
        parts = [f"fields {sorted(entry)}"]
        if isinstance(size, int) and size >= 0:
            parts.append(f"{size} bytes")
        if entry.get("mime"):
            parts.append(str(entry["mime"])[:80])
        parts.append(f"{len(cookies) if isinstance(cookies, list) else 0} cookies")
        if entry.get("incognito"):
            parts.append("private window")
        return ", ".join(parts)

    kind = msg.get("type")
    if kind == "offer":
        return item(msg.get("download"))
    if kind == "media":
        return item(msg.get("page"))
    if kind == "add":
        items = msg.get("items") if isinstance(msg.get("items"), list) else []
        first = f"; first: {item(items[0])}" if items else ""
        return f"{len(items)} links{' to choose from' if msg.get('pick') else ''}{first}"
    return "no details"


def _hello(msg: dict) -> dict:
    try:
        return ipc.request(msg, timeout=HELLO_TIMEOUT)
    except ipc.NotRunning:
        return _error("Downpour is not running", running=False)


def _outcome(kind: str, reply: dict) -> str:
    """A log summary that leaves out URLs, cookies and anything else private."""
    if not reply.get("ok"):
        return f"failed: {reply.get('error', 'no reason given')}"
    if kind == "offer":
        return "accepted" if reply.get("accept") else "declined"
    return "ok"


def serve(reader: BinaryIO, writer: BinaryIO) -> int:
    """Answer framed messages until the browser closes our input; exit status."""
    while True:
        try:
            payload = read_frame(reader)
        except MessageTooLarge as e:
            log.warning("%s", e)
            reply = _error(f"the message was too large ({e.length} bytes)")
        except ProtocolError as e:
            log.error("%s", e)
            return 1
        else:
            if payload is None:
                return 0
            try:
                msg = json.loads(payload.decode("utf-8"))
            except ValueError:
                log.warning("received a message that is not JSON")
                reply = _error("the message was not valid JSON")
            else:
                reply = handle(msg)
        try:
            write_message(writer, reply)
        except OSError:
            log.info("the browser closed the connection")
            return 0


def is_native_host_invocation(argv: Sequence[str]) -> bool:
    """True when a browser, not a person, started this process.

    Chromium browsers pass the calling extension's origin first
    ("chrome-extension://<id>/", then "--parent-window=<n>" on Windows).
    Firefox passes the path of the host manifest it read, then the calling
    extension's ID.
    """
    args = [str(arg) for arg in argv[1:]]
    if not args:
        return False
    if args[0].lower().startswith("chrome-extension://"):
        return True
    return any(arg == FIREFOX_EXTENSION_ID or _is_host_manifest(arg) for arg in args)


def _is_host_manifest(arg: str) -> bool:
    name = Path(arg).name.lower()
    return name.startswith(HOST_NAME) and name.endswith(".json")


def _caller(argv: Sequence[str]) -> str:
    args = [str(arg) for arg in argv[1:]]
    for arg in args:
        if arg.lower().startswith("chrome-extension://") or arg == FIREFOX_EXTENSION_ID:
            return arg
    return "an unknown caller"


def _setup_logging() -> None:
    """Append to native-host.log in the logs folder. Several hosts may run
    at once (one per message), so the file is shared and only rotated at
    start-up."""
    try:
        logs.setup(LOG_NAME, detailed=logs.detailed_setting(), shared=True)
    except OSError:
        logging.getLogger().addHandler(logging.NullHandler())
    logs.install_excepthooks()


def _std_stream(stream: object, fd: int, std_handle: int, mode: str) -> BinaryIO:
    """The binary side of a standard stream, even in a windowed build.

    A windowed (GUI-subsystem) executable may start without Python-level
    sys.stdin and sys.stdout although the browser handed it pipe handles;
    fall back to the descriptor, then to the Windows handle itself.
    """
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        return buffer
    try:
        return os.fdopen(fd, mode, buffering=0, closefd=False)
    except OSError:
        if sys.platform != "win32":
            raise
    import ctypes
    import msvcrt

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel32.GetStdHandle(std_handle)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError(f"standard handle {std_handle} is missing")
    flags = os.O_BINARY | (os.O_RDONLY if "r" in mode else os.O_WRONLY)
    return os.fdopen(msvcrt.open_osfhandle(handle, flags), mode, buffering=0)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _setup_logging()
    try:
        reader = _std_stream(sys.stdin, 0, -10, "rb")  # STD_INPUT_HANDLE
        writer = _std_stream(sys.stdout, 1, -11, "wb")  # STD_OUTPUT_HANDLE
    except OSError:
        log.exception("no standard input or output to talk to the browser over")
        return 1
    real_stdout = sys.stdout
    sys.stdout = sys.stderr if sys.stderr is not None else open(os.devnull, "w", encoding="utf-8")
    try:
        from downpour.core.winutil import parent_process

        parent = parent_process()
    except Exception:  # noqa: BLE001 - only for the log
        parent = "an unknown process"
    log.info(
        "helper %s (%s) started by %s from %s; %s",
        __version__, logs.build_id(), _caller(argv), parent, sys.executable,
    )
    try:
        return serve(reader, writer)
    finally:
        sys.stdout = real_stdout


if __name__ == "__main__":
    sys.exit(main(sys.argv))
