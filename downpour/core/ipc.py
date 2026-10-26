"""Talking to the running Downpour instance.

The running app owns a per-user named pipe. The pipe name is derived from a
random per-user secret, so another account cannot squat it, and every
connection must pass multiprocessing's HMAC challenge with that secret before
any payload is read. Payloads are UTF-8 JSON objects, never pickle.

Owning the pipe is also how single-instance works: a second copy of the app
fails to create it, forwards its command line to the first copy and exits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Callable

from downpour.core.paths import app_dir, data_dir

log = logging.getLogger(__name__)

MAX_MESSAGE = 16 * 1024 * 1024


class NotRunning(Exception):
    """No Downpour instance is listening."""


class AlreadyRunning(Exception):
    """Another Downpour instance already owns the pipe."""


def load_key(create: bool = False) -> bytes | None:
    path = data_dir() / "ipc.key"
    if create:
        try:
            with open(path, "xb") as f:
                f.write(secrets.token_bytes(32))
            log.info("created a new pipe key at %s", path)
        except FileExistsError:
            pass
    # A racing creator may not have finished writing yet.
    for _ in range(50):
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            return None
        if len(key) >= 32:
            return key
        time.sleep(0.01)
    # The file exists but never became a full key: it is corrupt (a kill during
    # the first write, or a bad disk), and would otherwise block every start
    # forever. Replace it rather than refuse to run.
    if create:
        key = secrets.token_bytes(32)
        tmp = path.with_suffix(".new")
        tmp.write_bytes(key)
        tmp.replace(path)
        log.warning("the pipe key at %s was too short to use; made a new one", path)
        return key
    raise OSError(f"{path} is unreadable")


def address(key: bytes) -> str:
    return r"\\.\pipe\downpour-" + hashlib.sha256(b"downpour-pipe:" + key).hexdigest()[:32]


Handler = Callable[[dict], dict]


class Server:
    """Accepts JSON requests on the pipe and answers them via ``handler``.

    The handler runs on a worker thread; anything touching the GUI must be
    marshalled by the handler itself.
    """

    def __init__(self, handler: Handler):
        self._handler = handler
        key = load_key(create=True)
        assert key is not None
        try:
            self._listener = Listener(address(key), family="AF_PIPE", authkey=key)
        except PermissionError as e:
            log.info("another copy of Downpour already owns the pipe")
            raise AlreadyRunning() from e
        log.info("listening for the browser helper and the command line on %s", address(key)[:30] + "...")
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="ipc-server", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._closed:
            try:
                conn = self._listener.accept()
            except AuthenticationError:
                if self._closed:
                    return
                log.warning("refused a pipe connection that failed the key check (an ipc.key from another account or copy?)")
                continue
            except (EOFError, OSError) as e:
                if self._closed:
                    return
                log.debug("a pipe connection failed before its request arrived: %r", e)
                continue
            threading.Thread(target=self._handle, args=(conn,), name="ipc-conn", daemon=True).start()

    def _handle(self, conn) -> None:
        started = time.monotonic()
        kind = "?"
        with conn:
            try:
                msg = json.loads(conn.recv_bytes(MAX_MESSAGE))
                if not isinstance(msg, dict):
                    raise ValueError("request must be a JSON object")
                kind = str(msg.get("type"))
                reply = self._handler(msg)
            except Exception as e:  # noqa: BLE001 - every failure becomes a reply
                log.exception("IPC request failed")
                reply = {"ok": False, "error": str(e)}
            try:
                conn.send_bytes(json.dumps(reply if reply is not None else {"ok": True}).encode())
            except OSError as e:
                log.info("could not send the answer to a %s request: %r", kind, e)
        ok = reply is None or bool(reply.get("ok", True))
        log.debug(
            "pipe request %s answered in %.0f ms: %s", kind, (time.monotonic() - started) * 1000,
            "ok" if ok else f"failed ({reply.get('error')})",
        )


def allow_foreground() -> None:
    """Let the app bring its window to the front for this request.

    Windows only lets the foreground process (or one it started, such as a
    native-messaging host started by the browser) take the foreground. Without
    this, a dialog the app opens in answer would flash in the taskbar instead
    of receiving focus, and the screen reader would never announce it.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except (AttributeError, OSError):
            pass


def request(msg: dict, timeout: float = 15.0) -> dict:
    """Send one request to the running app and return its reply."""
    key = load_key()
    if key is None:
        raise NotRunning("Downpour has never run on this account")
    allow_foreground()
    try:
        conn = Client(address(key), family="AF_PIPE", authkey=key)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise NotRunning("Downpour is not running") from e
    with conn:
        conn.send_bytes(json.dumps(msg).encode())
        if not conn.poll(timeout):
            log.warning("Downpour did not answer a %s request within %.0f seconds", msg.get("type"), timeout)
            raise TimeoutError("Downpour did not answer")
        reply = json.loads(conn.recv_bytes(MAX_MESSAGE))
    if not isinstance(reply, dict):
        raise ValueError("malformed reply")
    return reply


def launch_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return [str(windowless if windowless.exists() else exe), "-m", "downpour", *args]


def launch_app(*args: str) -> None:
    """Start the app detached from us.

    Browsers run native-messaging hosts inside a job object that is killed
    when the host exits, so the app must break away from that job or it dies
    with the host. Not every job allows breakaway; fall back if refused.
    """
    cmd = launch_command(*args)
    base = 0
    if sys.platform == "win32":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = str(app_dir())
    breakaway = base | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    for flags in (breakaway, base):
        try:
            proc = subprocess.Popen(
                cmd,
                creationflags=flags,
                cwd=str(app_dir()),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            log.info(
                "started Downpour as process %s: %s%s", proc.pid, " ".join(cmd),
                "" if flags == breakaway else " (could not leave the browser's job, so it may close with the browser)",
            )
            return
        except OSError as e:
            if flags == base:
                log.error("could not start Downpour with %s: %r", cmd, e)
                raise
            log.info("starting outside the browser's job was refused (%r); trying inside it", e)


def request_or_launch(
    msg: dict, launch_args: tuple[str, ...] = ("--tray",), wait: float = 30.0, timeout: float = 15.0
) -> dict:
    """Like ``request``, but starts the app first when it is not running.

    ``wait`` bounds the start-up; ``timeout`` bounds the answer itself.
    """
    try:
        return request(msg, timeout=timeout)
    except NotRunning:
        log.info("Downpour is not running; starting it for a %s request", msg.get("type"))
    launch_app(*launch_args)
    began = time.monotonic()
    deadline = began + wait
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            reply = request(msg, timeout=timeout)
        except NotRunning:
            continue
        log.info("Downpour answered %.1f seconds after starting", time.monotonic() - began)
        return reply
    log.warning("Downpour did not start within %.0f seconds", wait)
    raise NotRunning("Downpour did not start in time")
