"""Log files: where they live, what goes in them, and keeping secrets out.

Each part of Downpour writes its own file in ``<data dir>\\logs``:

* ``downpour.log`` - the window app: downloads, the queue, browser requests,
  settings changes and everything the user was shown as an error.
* ``native-host.log`` - the browser helper, one short-lived process per
  message from the extension.
* ``extension.log`` - what the browser extension saw and decided, which it
  sends through the helper.
* ``cli.log`` - ``downpour get`` runs.
* ``crash.log`` - Python's fault handler: the stack of every thread when a
  process dies inside native code, where no Python handler gets to run.

Detailed logging (the default) adds the HTTP exchanges, pipe requests,
yt-dlp's output, menu commands and the queue's decisions; with it off, the
files keep lifecycle events, warnings and errors. Files rotate by size, so
the folder never grows without bound either way.

Nothing secret is logged: no cookies, passwords, header values or proxy
credentials, and web addresses pass through ``redact_url``, which drops any
user:password part and masks query values that look like tokens or
signatures (signed download links are full of them).
"""

from __future__ import annotations

import faulthandler
import locale
import logging
import logging.handlers
import os
import platform
import re
import struct
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, quote, urlsplit

from downpour import APP_NAME, __version__
from downpour.core.paths import app_dir, data_dir, logs_dir

log = logging.getLogger(__name__)

FORMAT = "%(asctime)s.%(msecs)03d [%(process)d %(threadName)s] %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
SHARED_LIMIT = 1024 * 1024  # files that several processes append to
CRASH_LOG = "crash.log"
CLEAN_EXIT = "clean-exit"  # marker that the last run reached its shutdown (see mark_clean_exit)
EXTENSION_LOG = "extension.log"

# Query parameters whose values are credentials in signed or authenticated
# links (S3, Azure, Google Cloud, CloudFront, OAuth...). A name containing
# any of these words is masked.
_SECRET_WORDS = ("token", "sig", "secret", "pass", "pwd", "auth", "key", "session", "cred", "policy", "ticket")
_SECRET_NAMES = frozenset({"code", "sid", "hash", "otp", "jwt"})
_LONG_VALUE = 80  # longer values are trimmed whatever they hold
# A long run of letters and digits is a token or an ID whatever its name.
_TOKENISH = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-+/=.:%~]{24,}$")

_crash_file = None


# -- configuration ------------------------------------------------------------


class _RotatingFile(logging.handlers.RotatingFileHandler):
    """Rotates by size, but loses nothing when Windows refuses the rename
    because another process (or a virus scanner) has the file open: it keeps
    appending and tries again a minute later. The standard handler would
    instead drop every record until the rename finally worked."""

    _retry_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._retry_at = time.monotonic() + 60
            if self.stream is None:
                self.stream = self._open()


def _rotate_if_large(path: Path, limit: int) -> None:
    """For files several processes append to at once: start a new file when
    this one passed ``limit``. A failed rename (another process has the file
    open) is ignored; the next process to start will rotate it."""
    try:
        if path.stat().st_size > limit:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def setup(
    filename: str,
    *,
    detailed: bool = True,
    shared: bool = False,
    console_level: int | None = None,
) -> Path:
    """Send every logger to ``logs\\<filename>`` and return that path.

    ``shared`` is for files written by several short-lived processes at once
    (the browser helper): they append, and rotate only at start-up.
    ``console_level`` also prints records of that level and above to stderr.
    """
    path = logs_dir() / filename
    handler: logging.Handler
    try:
        if shared:
            _rotate_if_large(path, SHARED_LIMIT)
            handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        else:
            handler = _RotatingFile(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    root = logging.getLogger()
    for old in list(root.handlers):
        if getattr(old, "_downpour", False):
            root.removeHandler(old)
            old.close()
    handler._downpour = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if console_level is not None and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter("%(message)s"))
        console._downpour = True  # type: ignore[attr-defined]
        root.addHandler(console)
    set_detailed(detailed)
    logging.captureWarnings(True)
    return path


def set_detailed(on: bool) -> None:
    """Switch between detailed (DEBUG) and normal (INFO) logging at once."""
    logging.getLogger().setLevel(logging.DEBUG if on else logging.INFO)


def forced_detailed() -> bool:
    """True when DOWNPOUR_DEBUG=1 asks for detailed logs whatever the settings say."""
    return os.environ.get("DOWNPOUR_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def detailed_setting() -> bool:
    """The "detailed logging" setting, read straight from settings.json.

    For the browser helper, which starts once per message and must stay
    quick: it reads one key instead of building the whole Settings object.
    """
    if forced_detailed():
        return True
    try:
        import json

        value = json.loads((data_dir() / "settings.json").read_text(encoding="utf-8")).get("detailed_logging", True)
    except (OSError, ValueError, AttributeError):
        return True
    return value if isinstance(value, bool) else True


def install_excepthooks(on_error: Callable[[BaseException], None] | None = None) -> None:
    """Log every exception nothing else caught: on the main thread, on other
    threads, and in finalizers. ``on_error`` may also tell the user."""

    def excepthook(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, tb)
            return
        log.critical("unhandled error:\n%s", "".join(traceback.format_exception(kind, value, tb)))
        if on_error is not None:
            try:
                on_error(value)
            except Exception:  # noqa: BLE001 - never let the error handler fail
                pass

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(
            "unhandled error in thread %s:\n%s",
            args.thread.name if args.thread else "?",
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )

    def unraisable_hook(unraisable):
        log.warning(
            "ignored error in %r: %s",
            unraisable.object,
            "".join(traceback.format_exception(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook


def _clean_exit_marker() -> Path:
    return logs_dir() / CLEAN_EXIT


def mark_clean_exit() -> None:
    """Record that this run reached its shutdown. Windows can raise a handled
    COM disconnect (RPC_E_DISCONNECTED, "Windows fatal exception: code
    0x80010108") while it tears the process down at a normal exit, a log-off
    or a reboot; the fault handler writes that to crash.log even though
    nothing crashed. This marker lets the next start tell such teardown noise
    from a run that really died, so the user is not warned of a crash that
    did not happen."""
    try:
        _clean_exit_marker().write_text(datetime.now().strftime(DATE_FORMAT), encoding="utf-8")
    except OSError:
        pass


def enable_crash_log() -> None:
    """Have Python write every thread's stack to crash.log if the process
    dies in native code (wxWidgets, a DLL). A report left by the previous run
    is kept as crash.log.1. It is announced as a crash only when that run did
    not reach its shutdown; a report written while Windows tore the process
    down (see mark_clean_exit) is kept quietly instead of alarming the user
    at every start."""
    global _crash_file
    if _crash_file is not None:
        return
    marker = _clean_exit_marker()
    ended_cleanly = marker.exists()
    try:
        marker.unlink()
    except OSError:
        pass
    path = logs_dir() / CRASH_LOG
    try:
        if path.stat().st_size > 0:
            old = path.with_name(CRASH_LOG + ".1")
            os.replace(path, old)
            if ended_cleanly:
                log.debug("the previous run left a fault report written while shutting down; kept as %s", old)
            else:
                log.warning("an earlier run of Downpour crashed; its report is in %s", old)
    except OSError:
        pass
    try:
        _crash_file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - must stay open for the process's life
        faulthandler.enable(_crash_file, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        log.warning("could not enable the crash log", exc_info=True)


# -- what to say at start-up ----------------------------------------------------


def build_id() -> str:
    """The commit this copy was built from, as precisely as can be told."""
    if getattr(sys, "frozen", False):
        try:
            from downpour import _buildinfo  # written by tools/version_info.py at build time

            return str(_buildinfo.BUILD)
        except ImportError:
            return "build of unknown commit"
    git = app_dir() / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        commit = head
        if head.startswith("ref: "):
            ref = head[5:]
            ref_file = git / ref
            if ref_file.exists():
                commit = ref_file.read_text(encoding="utf-8").strip()
            else:
                packed = (git / "packed-refs").read_text(encoding="utf-8").splitlines()
                commit = next(line.split()[0] for line in packed if line.endswith(" " + ref))
        return f"source {commit[:7]}"
    except (OSError, StopIteration, IndexError):
        return "source"


def banner(component: str, argv: list[str] | None = None) -> None:
    """Log who is running where: the first thing to check in any report."""
    frozen = bool(getattr(sys, "frozen", False))
    try:
        loc = locale.getlocale()[0] or "unknown"
    except ValueError:
        loc = "unknown"
    level = "detailed" if logging.getLogger().isEnabledFor(logging.DEBUG) else "normal"
    log.info("===== %s %s (%s) starting: %s =====", APP_NAME, __version__, build_id(), component)
    log.info(
        "program %s (%s), Python %s %d-bit",
        sys.executable, "built" if frozen else "from source", platform.python_version(), struct.calcsize("P") * 8,
    )
    log.info(
        "%s, locale %s, encoding %s, %d processors",
        windows_version(), loc, locale.getpreferredencoding(False), os.cpu_count() or 0,
    )
    log.info("data %s, logs %s, logging %s", data_dir(), logs_dir(), level)
    if argv:
        log.info("arguments: %s", " ".join(redact_arg(a) for a in argv))


def windows_version() -> str:
    """Such as "Windows 10.0.26200.6584 (25H2, Core)". Read from the
    registry because platform.platform() asks WMI, which can take a second."""
    if sys.platform != "win32":
        return platform.system()
    v = sys.getwindowsversion()
    text = f"Windows {v.major}.{v.minor}.{v.build}"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            found = {}
            for name in ("UBR", "DisplayVersion", "EditionID"):
                try:
                    found[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
    except OSError:
        return text
    if "UBR" in found:
        text += f".{found['UBR']}"
    extra = [str(found[n]) for n in ("DisplayVersion", "EditionID") if found.get(n)]
    return f"{text} ({', '.join(extra)})" if extra else text


def thread_stacks() -> str:
    """What every thread is doing right now, for hang reports."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():  # noqa: SLF001 - the documented way to do this
        parts.append(f"--- {names.get(ident, ident)}\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(parts)


class Watchdog:
    """Notices when the window's thread stops answering and logs what every
    thread was doing at the time - evidence a hang otherwise never leaves.

    ``post`` runs a function on the watched thread (wx.CallAfter for the
    window); this module stays free of wx.
    """

    def __init__(self, post: Callable[[Callable[[], None]], object], timeout: float = 30.0, interval: float = 5.0):
        self._post = post
        self._timeout = timeout
        self._interval = interval
        self._beat = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _alive(self) -> None:
        self._beat = time.monotonic()

    def _run(self) -> None:
        reported = False
        last = time.monotonic()
        while not self._stop.wait(self._interval):
            now = time.monotonic()
            if now - last > self._interval * 4:  # the computer was asleep: start afresh
                self._beat = now
                reported = False
            last = now
            try:
                self._post(self._alive)
            except Exception:  # noqa: BLE001 - the app is closing
                return
            silent = now - self._beat
            if silent > self._timeout and not reported:
                reported = True
                log.warning(
                    "the window has not responded for %.0f seconds; every thread's stack follows\n%s",
                    silent, thread_stacks(),
                )
            elif reported and silent < self._interval * 2:
                reported = False
                log.warning("the window is responding again")


# -- keeping secrets out ----------------------------------------------------------


def _secret_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SECRET_NAMES or any(word in lowered for word in _SECRET_WORDS)


def redact_url(url: str) -> str:
    """``url`` fit for a log: no user:password, no fragment, and no values
    of query parameters that look like credentials.

    Parameter names stay, so a log still shows that a link was signed and
    when it expired (``se=``, ``X-Amz-Date=``), just not the signature.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
        netloc = parts.hostname or ""
        if ":" in netloc:
            netloc = f"[{netloc}]"
        if parts.port:
            netloc += f":{parts.port}"
    except ValueError:
        return "<unreadable address>"
    query = ""
    if parts.query:
        pairs = []
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            if _secret_name(name):
                value = "<hidden>"
            elif len(value) > _LONG_VALUE or _TOKENISH.match(value):
                value = f"<{len(value)} characters>"
            pairs.append(f"{quote(name, safe='')}={quote(value, safe='<>:/ ')}")
        query = "?" + "&".join(pairs)
    path = parts.path
    if len(path) > 300:
        path = path[:300] + f"...<{len(parts.path)} characters>"
    return f"{parts.scheme}://{netloc}{path}{query}" if parts.scheme else f"{netloc}{path}{query}"


def redact_arg(arg: str) -> str:
    """A command-line argument for the log: web addresses redacted."""
    text = str(arg)
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", text):
        return redact_url(text)
    return text


def describe_error(error: BaseException | None) -> str:
    """The exception's class and message, plus the Windows or errno code:
    more than the friendly text the user sees, which is the point."""
    if error is None:
        return "no error recorded"
    parts = [type(error).__name__]
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    if code:
        parts.append(f"code {code}")
    reason = getattr(error, "reason", None)
    if isinstance(reason, BaseException):
        parts.append(f"reason {type(reason).__name__}: {reason}")
    return f"{error} [{', '.join(parts)}]"


def summarize_cookies(cookies: list | None) -> str:
    """How many cookies and for which domains - never names or values."""
    if not cookies:
        return "no cookies"
    domains = sorted({str(c.get("domain", "")).lstrip(".") for c in cookies if isinstance(c, dict)})
    shown = ", ".join(domains[:5]) + (f" and {len(domains) - 5} more" if len(domains) > 5 else "")
    return f"{len(cookies)} cookie{'s' if len(cookies) != 1 else ''} for {shown or 'unknown domains'}"


# -- the extension's log, which arrives through the browser helper ----------------


LEVELS = {"debug": "DEBUG", "info": "INFO", "warn": "WARNING", "warning": "WARNING", "error": "ERROR"}
MAX_ENTRIES = 500
MAX_ENTRY = 2000


def extension_lines(entries: object, source: str) -> list[str]:
    """Turn the entries of an extension ``log`` message into log-file lines.

    Each entry is ``{"t": ISO time, "level": "info", "msg": text}``. Anything
    malformed is skipped rather than trusted; times become local time so the
    file lines up with the others.
    """
    if not isinstance(entries, list):
        return []
    lines = []
    for entry in entries[:MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        message = str(entry.get("msg", "")).replace("\r", " ").replace("\n", " | ")[:MAX_ENTRY]
        if not message:
            continue
        level = LEVELS.get(str(entry.get("level", "info")).lower(), "INFO")
        try:
            stamp = datetime.fromisoformat(str(entry.get("t", ""))).astimezone()
            when = stamp.strftime(DATE_FORMAT) + f".{stamp.microsecond // 1000:03d}"
        except ValueError:
            when = datetime.now().strftime(DATE_FORMAT) + ".000"
        lines.append(f"{when} [{source}] {level} {message}")
    return lines


def append_extension_log(lines: list[str]) -> Path:
    """Append lines to extension.log in one write; rotate when it grows big."""
    path = logs_dir() / EXTENSION_LOG
    _rotate_if_large(path, SHARED_LIMIT)
    if lines:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return path
