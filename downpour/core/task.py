"""One running HTTP download: a controller thread and its connections.

The controller probes the server, prepares the file, then supervises worker
threads, one per connection. Each worker streams bytes into its own segment
of the file; when it finishes, it takes half of the biggest remaining segment
(see segments.py), so the download ends with every connection busy instead
of one slow straggler. The controller adds connections, measures speed,
checkpoints progress and decides when the download has failed.

Durability rule: saved progress must never claim bytes that are not on disk.
A checkpoint snapshots the segment positions, then fsyncs, then saves the
snapshot - a crash can lose a few seconds of progress but cannot corrupt the
file.

Retry policy: any byte arriving resets the failure count. Transient failures
(network errors, 5xx, 429) back off exponentially up to 30 seconds and the
download fails only after ``max_retries`` failures in a row. A server that
refuses an extra connection outright just lowers this download's connection
count; it only fails the download when no other connection is left.
"""

from __future__ import annotations

import collections
import enum
import logging
import os
import shutil
import ssl
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from downpour.core import checksum, net, winutil
from downpour.core import segments as seglib
from downpour.core.limiter import RateLimiter
from downpour.core.logs import describe_error, redact_url, summarize_cookies
from downpour.core.models import Download, Status
from downpour.core.segments import Segment
from downpour.core.units import format_duration, format_size, format_speed

if TYPE_CHECKING:
    from downpour.core.engine import Engine

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
TICK = 0.25
CHECKPOINT_SECONDS = 5.0
SPEED_WINDOW = 5.0
NO_SPLIT = 1 << 62
REFUSAL_RETRIES = 3


DONE, MORE, BROKEN = "done", "more", "broken"  # how a response ended


class Outcome(enum.Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class DownloadError(Exception):
    """A failure that ends the download until the user retries it."""


class DiskError(DownloadError):
    pass


class Stopped(Exception):
    """The task was asked to stop."""


def backoff(failures: int) -> float:
    return min(30.0, 2.0 ** min(failures, 5))


def _is_certificate_error(error: BaseException) -> bool:
    reason = getattr(error, "reason", error)
    return isinstance(reason, ssl.SSLCertVerificationError)


class HttpTask:
    """Downloads one HTTP or HTTPS resource. Created and driven by the Engine."""

    def __init__(self, engine: "Engine", dl: Download):
        self.engine = engine
        self.dl = dl
        # Fresh objects: a connection of an earlier, stopped task that has not
        # exited yet must never touch segments this task owns.
        dl.segments = [Segment(s.start, s.end, s.pos) for s in dl.segments]
        self.stop_status = Status.PAUSED
        self._stop = threading.Event()
        self._lock = threading.Lock()  # segments, file writes, counters
        self._fh = None
        self._opener = None
        self._fetch_url = dl.url
        self._responses: set = set()
        self._workers: list[threading.Thread] = []
        self._limiter = RateLimiter(dl.speed_limit)
        self._fatal: DownloadError | None = None
        self._failures = 0
        self._refusals = 0  # outright refusals while no other connection ran
        self._last_error: BaseException | None = None
        self._next_spawn = 0.0
        self._max_conn = 1
        self._received = 0
        self._started = 0.0
        self._slow_since: float | None = None  # when the speed first dropped below the stall threshold
        self._reconnecting = False  # connections were cut on purpose; their errors are not failures
        self._peak_connections = 0
        self._samples: collections.deque[tuple[float, int]] = collections.deque()
        self.checkpointed: list | None = None  # last segment snapshot known to be on disk
        self.thread = threading.Thread(target=self._run, name=f"download-{dl.id}", daemon=True)

    # -- control, called by the engine ---------------------------------------

    def start(self) -> None:
        self.thread.start()

    def stop(self, status: Status = Status.PAUSED) -> None:
        self.stop_status = status
        self._stop.set()
        self._abort_all()

    def set_speed_limit(self, rate: int) -> None:
        self._limiter.set_rate(rate)

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- lifecycle ------------------------------------------------------------

    def _run(self) -> None:
        dl = self.dl
        outcome, error = Outcome.STOPPED, ""
        self._started = time.monotonic()
        self._log_start()
        try:
            first = self._prepare()
            # Seed the checkpoint with the state just established: for a fresh
            # download that is byte 0 (nothing on disk yet), for a resumed one
            # the fsynced positions. A save before the first real checkpoint
            # (e.g. the user reorders or edits the download in its first few
            # seconds) then persists this durable state, never the live, not-
            # yet-fsynced positions that would leave a silent gap after a crash.
            with self._lock:
                self.checkpointed = [s.to_list() for s in dl.segments]
            if first is not None:
                self._transfer(*first)
            self._finish()
            outcome = Outcome.COMPLETED
        except Stopped:
            outcome = Outcome.STOPPED
        except net.HttpError as e:
            outcome, error = Outcome.FAILED, net.describe(e)
            log.info("download %s: the server refused it: %s", dl.id, describe_error(e))
        except DownloadError as e:
            outcome, error = Outcome.FAILED, str(e)
            if e.__cause__ is not None:
                log.info("download %s: caused by %s", dl.id, describe_error(e.__cause__))
        except OSError as e:
            outcome, error = Outcome.FAILED, f"File error: {e.strerror or e}"
            log.warning("download %s: file error with %s", dl.id, dl.part_path or dl.directory, exc_info=True)
        except Exception as e:  # noqa: BLE001 - a crash must still end the task cleanly
            log.exception("download %s crashed", dl.id)
            outcome, error = Outcome.FAILED, f"Unexpected error: {e}"
        finally:
            self._shutdown_workers()
            self._close_file(checkpoint=outcome is not Outcome.COMPLETED)
            dl.speed, dl.eta, dl.active_connections = 0.0, None, 0
        self._log_end(outcome, error)
        self.engine._task_done(self, outcome, error)

    def _log_start(self) -> None:
        dl = self.dl
        settings = self.engine.settings
        done = seglib.downloaded(dl.segments) if dl.segments else 0
        log.info(
            "download %s: starting %s from %s; %s of %s on disk, up to %d connections, %s",
            dl.id, dl.filename or "(name not known yet)", redact_url(dl.url), format_size(done),
            format_size(dl.size) if dl.size is not None else "unknown size", self._connection_limit(),
            f"limited to {format_speed(dl.speed_limit)}" if dl.speed_limit else "no speed limit of its own",
        )
        log.debug(
            "download %s: %s, %s, sign-in %s, %s, checksum %s, user agent %s, proxy %s, timeout %s s, %d retries",
            dl.id, summarize_cookies(dl.cookies),
            f"referrer {redact_url(dl.referrer)}" if dl.referrer else "no referrer",
            "yes" if dl.username else "no",
            f"extra headers {sorted(dl.headers)}" if dl.headers else "no extra headers",
            dl.checksum.split(":", 1)[0] if dl.checksum else "none",
            dl.user_agent or settings.user_agent, settings.proxy_mode, settings.timeout, settings.max_retries,
        )

    def _log_end(self, outcome: Outcome, error: str) -> None:
        dl = self.dl
        took = time.monotonic() - self._started
        received = self._received
        speed = format_speed(received / took) if took > 0 and received else "nothing received"
        if outcome is Outcome.COMPLETED:
            peak = self._peak_connections
            log.info(
                "download %s: completed %s, %s in %s (%s this run, %s), %d connection%s at most",
                dl.id, dl.path, format_size(dl.size), format_duration(took), format_size(received),
                speed, peak, "" if peak == 1 else "s",
            )
        elif outcome is Outcome.FAILED:
            log.warning(
                "download %s: failed after %s with %s received (%s on disk): %s",
                dl.id, format_duration(took), format_size(received), format_size(dl.downloaded), error,
            )
        else:
            log.info(
                "download %s: stopped (%s) after %s, %s received, %s on disk",
                dl.id, self.stop_status.value, format_duration(took), format_size(received),
                format_size(dl.downloaded),
            )

    def _prepare(self) -> tuple[Segment, object, net.ResponseInfo] | None:
        """Connect and open the file. Returns the first segment and its open
        response, or None when the bytes are already all on disk."""
        dl = self.dl
        settings = self.engine.settings
        self.engine._set_status(dl, Status.CONNECTING)
        self._opener = net.build_opener(
            url=dl.url,
            cookies=dl.cookies,
            username=dl.username,
            password=dl.password,
            proxy_mode=settings.proxy_mode,
            proxy_url=settings.proxy_url,
        )
        if self._has_partial():
            if seglib.all_done(dl.segments):
                self._open_file(create=False)
                return None
            seg = next(s for s in dl.segments if not s.done)
            resp, info = self._connect(seg.pos, if_range=net.resume_validator(dl.etag, dl.last_modified))
            if info.ranged and info.start == seg.pos and info.total == dl.size:
                self._fetch_url = info.url
                if not (dl.etag or dl.last_modified):  # cleared when the address was renewed
                    dl.etag, dl.last_modified = info.etag, info.last_modified
                self._open_file(create=False)
                self._max_conn = self._connection_limit()
                with self._lock:
                    seg.active = True
                log.info("download %s: resuming at byte %s of %s", dl.id, seg.pos, dl.size)
                return seg, resp, info
            log.info(
                "download %s: the server no longer matches the partial file (HTTP %s, %s, starts at %s, "
                "file size %s, expected %s); starting over",
                dl.id, info.status, "ranged" if info.ranged else "not ranged", info.start, info.total, dl.size,
            )
            if info.status == 200:
                return self._start_fresh(resp, info)  # already the whole file from byte 0
            self._discard(resp)
        return self._start_fresh()

    def _has_partial(self) -> bool:
        dl = self.dl
        if not (dl.resumable and dl.size is not None and dl.segments and dl.filename):
            return False
        try:
            on_disk = os.path.getsize(dl.part_path)
        except OSError:
            log.info("download %s: no partial file at %s, so it starts from the beginning", dl.id, dl.part_path)
            return False
        if on_disk != dl.size:
            log.info("download %s: the partial file is %s bytes, expected %s; starting over", dl.id, on_disk, dl.size)
        return on_disk == dl.size

    def _start_fresh(self, resp=None, info: net.ResponseInfo | None = None) -> tuple[Segment, object, net.ResponseInfo]:
        dl = self.dl
        settings = self.engine.settings
        if resp is None:
            try:
                resp, info = self._connect(0)
            except net.HttpError as e:
                if e.status != 416:
                    raise
                # A range request against an empty file can only fail; ask plainly.
                resp, info = self._connect(None)
        assert info is not None
        if info.ranged and info.total is None:
            # A bounded range with no total (Content-Range ".../*"): its EOF would
            # look like the end of the file and could leave a silently short file.
            # Ask again with no range so the server sends the whole body (a 200).
            log.info("download %s: the server gave a partial range but hid the total; re-requesting the whole file", dl.id)
            self._discard(resp)
            resp, info = self._connect(None)
        self._fetch_url = info.url
        size = info.total if info.ranged else info.length
        dl.size = size
        dl.resumable = size is not None and (info.ranged or info.accept_ranges)
        dl.mime = info.mime or dl.mime
        dl.etag, dl.last_modified = info.etag, info.last_modified
        if not (dl.filename_fixed and dl.filename):
            dl.filename = net.pick_name(info, dl.url)
        folder = Path(dl.directory) if dl.directory else settings.folder_for(dl.filename, dl.url)
        folder.mkdir(parents=True, exist_ok=True)
        wanted = folder / dl.filename
        if settings.duplicate_action == "skip" and wanted.exists() and (dl.path or "").lower() != str(wanted).lower():
            self._discard(resp)
            raise DownloadError(f"Skipped: {wanted} already exists. Change the duplicate setting in Options to download it anyway.")
        final = self.engine.claim_path(dl, wanted)
        dl.directory, dl.filename = str(final.parent), final.name
        if size:
            free = shutil.disk_usage(folder).free
            if free < size:
                self._discard(resp)
                raise DownloadError(
                    f"Not enough disk space: the file needs {format_size(size)}, "
                    f"{format_size(free)} is free"
                )
        self._open_file(create=True, size=size)
        with self._lock:
            dl.segments = [Segment(0, size, 0, active=True)]
            dl.downloaded = 0
        self.engine.save(dl)
        self._max_conn = self._connection_limit() if dl.resumable else 1
        log.info(
            "download %s: %s, %s, %s, type %s, saving in %s",
            dl.id, dl.filename, format_size(size), "resumable" if dl.resumable else "not resumable",
            dl.mime or "unknown", dl.directory,
        )
        if info.url != dl.url:
            log.info("download %s: the link leads to %s", dl.id, redact_url(info.url))
        if info.disposition:
            log.debug("download %s: the server named it %r", dl.id, info.filename)
        return dl.segments[0], resp, info

    def _connection_limit(self) -> int:
        n = self.dl.connections or self.engine.settings.max_connections
        return max(1, min(32, n))

    def _split_size(self) -> int:
        return self.engine.settings.min_split_size if self.dl.resumable else NO_SPLIT

    # -- transfer -------------------------------------------------------------

    def _transfer(self, seg: Segment, resp, info: net.ResponseInfo) -> None:
        dl = self.dl
        self.engine._set_status(dl, Status.DOWNLOADING)
        self._spawn(seg, (resp, info))
        last_checkpoint = time.monotonic()
        while True:
            now = time.monotonic()
            new: list[Segment] = []
            with self._lock:
                busy = sum(s.active for s in dl.segments)
                complete = seglib.all_done(dl.segments)
                dl.downloaded = seglib.downloaded(dl.segments)
                dl.active_connections = busy
                self._peak_connections = max(self._peak_connections, busy)
                failures = self._failures
                # Open every allowed connection at once, as IDM and aria2 do;
                # after a failure, wait out the backoff first.
                if not complete and now >= self._next_spawn:
                    while busy + len(new) < self._max_conn:
                        extra = seglib.acquire(dl.segments, self._split_size())
                        if extra is None:
                            break
                        new.append(extra)
            self._measure(now)
            if complete and busy == 0:
                return
            if busy == 0:
                self._reconnecting = False
            self._watch_for_stall(now, busy)
            if failures > self.engine.settings.max_retries:
                for extra in new:
                    self._release(extra)
                raise DownloadError(
                    f"Gave up after {failures} failed attempts. Last error: {net.describe(self._last_error)}"
                )
            for extra in new:
                self._spawn(extra)
            if now - last_checkpoint >= CHECKPOINT_SECONDS:
                self._checkpoint()
                last_checkpoint = now
            if self._stop.wait(TICK):
                break
        if self._fatal is not None:
            raise self._fatal
        raise Stopped()

    def _spawn(self, seg: Segment, opened: tuple | None = None) -> None:
        t = threading.Thread(
            target=self._worker, args=(seg, opened), name=f"download-{self.dl.id}-conn", daemon=True
        )
        self._workers.append(t)
        t.start()

    def _worker(self, seg: Segment | None, opened: tuple | None) -> None:
        resp = None
        try:
            while seg is not None:
                if opened is None:
                    opened = self._open_segment(seg)
                    if opened is None:
                        return  # the segment was handed back
                resp, info = opened
                ended = self._pump(seg, resp, info.length)
                log.debug("download %s: connection %s at byte %s", self.dl.id, ended, seg.pos)
                self._discard(resp)
                resp = opened = None
                if ended == BROKEN:
                    return
                if ended == MORE:
                    continue  # the server sent less than asked for; ask for the rest
                seg = self._next_work(seg)
        except DownloadError as e:
            self._fail(e)
        except Exception as e:  # noqa: BLE001
            log.exception("download %s: connection crashed", self.dl.id)
            self._fail(DownloadError(f"Unexpected error: {e}"))
        finally:
            if resp is not None:
                self._discard(resp)
            if seg is not None:
                self._release(seg)

    def _next_work(self, seg: Segment) -> Segment | None:
        with self._lock:
            seg.active = False
            if self._stop.is_set():
                return None
            busy = sum(s.active for s in self.dl.segments)
            if busy >= self._max_conn:
                return None
            return seglib.acquire(self.dl.segments, self._split_size())

    def _open_segment(self, seg: Segment) -> tuple | None:
        """Open a connection for ``seg``. On failure, hand the segment back
        and return None."""
        dl = self.dl
        with self._lock:
            if dl.resumable:
                start, end = seg.pos, seg.end
            else:
                seg.pos = seg.start  # no ranges: every attempt starts over
                start, end = None, None
        urls = [self._fetch_url] + ([dl.url] if dl.url != self._fetch_url else [])
        for attempt, url in enumerate(urls):
            try:
                resp, info = net.open_range(
                    self._opener, url, start=start, end=end,
                    user_agent=dl.user_agent or self.engine.settings.user_agent,
                    referrer=dl.referrer, headers=dl.headers, timeout=self.engine.settings.timeout,
                )
            except net.HttpError as e:
                if e.transient:
                    self._note_failure(e)
                    self._release(seg, ration=e.status in (429, 503))
                    return None
                if attempt + 1 < len(urls):
                    continue  # the redirect target may have expired; ask the original link again
                self._refused(seg, e)
                return None
            except net.NETWORK_ERRORS as e:
                if _is_certificate_error(e):
                    raise DownloadError(net.describe(e)) from e
                if not self._stop.is_set() and not self._reconnecting:
                    self._note_failure(e)
                self._release(seg)
                return None
            try:
                usable = self._fits(info, start)
            except DownloadError:
                resp.close()
                raise
            if not usable:
                resp.close()
                self._refused(seg, None)
                return None
            self._fetch_url = info.url
            with self._lock:
                self._responses.add(resp)
            if self._stop.is_set():
                net.abort(resp)
            log.debug("download %s: connection open for bytes %s to %s", dl.id, start, end if end is not None else "end")
            return resp, info
        return None

    def _fits(self, info: net.ResponseInfo, start: int | None) -> bool:
        """Is this response the bytes we asked for?"""
        dl = self.dl
        if info.total is not None and dl.size is not None and info.total != dl.size:
            raise DownloadError("The file changed on the server while downloading. Use Redownload to start again.")
        if start is None:
            return info.status == 200 or (info.ranged and info.start == 0)
        if info.ranged:
            return info.start == start
        return info.status == 200 and start == 0

    def _pump(self, seg: Segment, resp, expected: int | None) -> str:
        """Copy the response into the file.

        Returns DONE once the segment is complete, MORE when the response
        ended cleanly short of the segment (some servers cap range sizes),
        and BROKEN when the connection failed or the task is stopping.
        """
        got = 0
        hint = min(self._limiter.chunk_hint(CHUNK), self.engine.limiter.chunk_hint(CHUNK))
        while not self._stop.is_set():
            want = hint
            with self._lock:
                if seg.end is not None:
                    left = seg.end - seg.pos
                    if left <= 0:
                        return DONE
                    want = min(want, left)
            try:
                data = resp.read1(want)
            except net.NETWORK_ERRORS as e:
                if not self._stop.is_set() and not self._reconnecting:
                    self._note_failure(e)
                return BROKEN
            if not data:
                with self._lock:
                    if seg.end is None:  # unknown length: the end of the stream is the end of the file
                        seg.end = seg.pos
                        return DONE
                    if seg.pos >= seg.end:
                        return DONE
                if self._stop.is_set():
                    return BROKEN
                if got and expected is not None and got >= expected and self.dl.resumable:
                    return MORE
                self._note_failure(ConnectionResetError("the server closed the connection early"))
                return BROKEN
            got += len(data)
            with self._lock:
                n = len(data) if seg.end is None else min(len(data), seg.end - seg.pos)
                if n > 0:
                    try:
                        self._fh.seek(seg.pos)
                        self._fh.write(data if n == len(data) else data[:n])
                    except OSError as e:
                        raise DiskError(f"Could not write the file: {e.strerror or e}") from e
                    seg.pos += n
                    self._received += n
                    self._failures = 0
                    self._refusals = 0
            if n > 0:
                self._limiter.throttle(n, self._stop)
                self.engine.limiter.throttle(n, self._stop)
            hint = min(self._limiter.chunk_hint(CHUNK), self.engine.limiter.chunk_hint(CHUNK))
        return BROKEN

    def _watch_for_stall(self, now: float, busy: int) -> None:
        """A resumable download that crawls for too long is reconnected: its
        connections are cut, which makes the workers hand their segments
        back, and the loop reopens them after the usual backoff."""
        settings = self.engine.settings
        if not (self.dl.resumable and busy and settings.stall_speed > 0 and settings.stall_seconds > 0):
            self._slow_since = None
            return
        if self.dl.speed >= settings.stall_speed or now - self._started < SPEED_WINDOW:
            self._slow_since = None
            return
        if self._slow_since is None:
            self._slow_since = now
            return
        if now - self._slow_since >= settings.stall_seconds:
            log.info(
                "download %s: only %s for %d seconds; reconnecting",
                self.dl.id, format_speed(self.dl.speed), settings.stall_seconds,
            )
            self._slow_since = None
            with self._lock:
                self._reconnecting = True
                self._next_spawn = now + 1.0
            self._abort_all()

    # -- failure bookkeeping ----------------------------------------------------

    def _note_failure(self, error: BaseException) -> None:
        with self._lock:
            self._failures += 1
            self._last_error = error
            wait = backoff(self._failures)
            self._next_spawn = time.monotonic() + wait
            count = self._failures
        log.info(
            "download %s: attempt failed (%d of %d allowed, next try in %.0f s): %s",
            self.dl.id, count, self.engine.settings.max_retries, wait, describe_error(error),
        )

    def _release(self, seg: Segment, ration: bool = False) -> None:
        """Hand a segment back so another connection can take it."""
        with self._lock:
            if seg.active:
                seg.active = False
                if not self.dl.resumable and not seg.done:
                    seg.pos = seg.start
            if ration:
                # The server is rationing connections; stay at what it allows.
                busy = sum(s.active for s in self.dl.segments)
                if busy:
                    self._max_conn = max(1, busy)

    def _refused(self, seg: Segment, error: BaseException | None) -> None:
        """A server said no to a connection outright (403, ranges ignored...).

        With other connections still running, it is rationing connections, so
        stay at what it allows. With none running, it may only be counting
        connections that are still closing, so if this download has received
        data, retry a few times before believing it.
        """
        with self._lock:
            seg.active = False
            busy = sum(s.active for s in self.dl.segments)
            if busy:
                self._max_conn = max(1, busy)
                log.info(
                    "download %s: server refused another connection (%s); staying at %d",
                    self.dl.id, net.describe(error) if error else "range ignored", busy,
                )
                return
            self._refusals += 1
            retry = self._received > 0 and self._refusals <= REFUSAL_RETRIES
        if retry:
            self._note_failure(error or ConnectionRefusedError("range ignored"))
            return
        if error is not None:
            self._fail(DownloadError(net.describe(error)))
        else:
            self._fail(DownloadError("The server stopped accepting resumed downloads. Try again later, or use Redownload."))

    def _fail(self, error: DownloadError) -> None:
        log.info("download %s: giving up: %s", self.dl.id, error)
        with self._lock:
            if self._fatal is None:
                self._fatal = error
        self._stop.set()
        self._abort_all()

    # -- I/O helpers ----------------------------------------------------------

    def _connect(self, start: int | None, if_range: str = ""):
        """Open the first connection, retrying transient failures."""
        dl = self.dl
        settings = self.engine.settings
        while True:
            if self._stop.is_set():
                raise Stopped()
            try:
                resp, info = net.open_range(
                    self._opener, dl.url, start=start,
                    user_agent=dl.user_agent or settings.user_agent,
                    referrer=dl.referrer, headers=dl.headers, timeout=settings.timeout,
                    if_range=if_range,
                )
            except net.HttpError as e:
                if not e.transient:
                    raise
                self._note_failure(e)
            except net.NETWORK_ERRORS as e:
                if self._stop.is_set():
                    raise Stopped() from e
                if _is_certificate_error(e):
                    raise DownloadError(net.describe(e)) from e
                self._note_failure(e)
            else:
                with self._lock:
                    self._responses.add(resp)
                if self._stop.is_set():
                    self._discard(resp)
                    raise Stopped()
                return resp, info
            if self._failures > settings.max_retries:
                raise DownloadError(
                    f"Gave up after {self._failures} failed attempts. Last error: {net.describe(self._last_error)}"
                )
            if self._stop.wait(backoff(self._failures)):
                raise Stopped()

    def _open_file(self, create: bool, size: int | None = None) -> None:
        path = self.dl.part_path
        if create:
            fh = open(path, "w+b", buffering=0)
            if size:
                winutil.set_sparse(fh)
                fh.truncate(size)
        else:
            fh = open(path, "r+b", buffering=0)
        self._fh = fh

    def _checkpoint(self) -> None:
        dl = self.dl
        with self._lock:
            dl.segments = seglib.compact(dl.segments)
            snapshot = [s.to_list() for s in dl.segments]
            dl.downloaded = seglib.downloaded(dl.segments)
        if self._fh is not None:
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                log.warning("download %s: fsync failed", dl.id, exc_info=True)
                return
        self.checkpointed = snapshot
        self.engine.save(dl, segments=snapshot)

    def _close_file(self, checkpoint: bool) -> None:
        if self._fh is None:
            return
        if checkpoint:
            self._checkpoint()  # workers have stopped, so this is exact
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def _discard(self, resp) -> None:
        with self._lock:
            self._responses.discard(resp)
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    def _abort_all(self) -> None:
        with self._lock:
            live = list(self._responses)
        for resp in live:
            net.abort(resp)

    def _shutdown_workers(self) -> None:
        self._stop.set()
        self._abort_all()
        for t in self._workers:
            t.join(timeout=15)

    def _measure(self, now: float) -> None:
        dl = self.dl
        self._samples.append((now, self._received))
        while len(self._samples) > 2 and now - self._samples[0][0] > SPEED_WINDOW:
            self._samples.popleft()
        t0, b0 = self._samples[0]
        dl.speed = (self._received - b0) / (now - t0) if now > t0 else 0.0
        if dl.size and dl.speed > 0:
            dl.eta = max(0.0, (dl.size - dl.downloaded) / dl.speed)
        else:
            dl.eta = None

    # -- completion -----------------------------------------------------------

    def _finish(self) -> None:
        dl = self.dl
        settings = self.engine.settings
        self.engine._set_status(dl, Status.FINISHING)
        part = dl.part_path
        with self._lock:
            dl.downloaded = seglib.downloaded(dl.segments)
            if dl.size is None:
                dl.size = dl.downloaded
        fh = self._fh
        if fh is not None:
            if os.fstat(fh.fileno()).st_size != dl.size:
                fh.truncate(dl.size)
            os.fsync(fh.fileno())
            winutil.set_sparse(fh, False)
            fh.close()
            self._fh = None
        dl.segments = [Segment(0, dl.size, dl.size)]
        dl.downloaded = dl.size or 0
        dl.speed, dl.eta = 0.0, None

        if dl.checksum:
            parsed = checksum.parse(dl.checksum)
            if parsed is not None:
                algo, expected = parsed
                try:
                    actual = checksum.compute(part, algo, stop=self._stop)
                except InterruptedError as e:
                    raise Stopped() from e
                dl.checksum_ok = actual == expected
                log.info(
                    "download %s: %s checksum %s", dl.id, algo.upper(),
                    "matches" if dl.checksum_ok else f"is {actual}, expected {expected}",
                )
                if not dl.checksum_ok:
                    raise DownloadError(
                        f"Checksum mismatch: expected {algo.upper()} {expected}, got {actual}. "
                        "The file may be corrupt; use Redownload."
                    )
        if self._stop.is_set():
            raise Stopped()

        final = Path(dl.path)
        if final.exists() and settings.duplicate_action != "overwrite":
            final = self.engine.claim_path(dl, final)  # something took the name meanwhile
            dl.directory, dl.filename = str(final.parent), final.name
        try:
            os.replace(part, final)
        except OSError as e:
            raise DownloadError(f"Could not rename the finished file: {e.strerror or e}") from e
        log.debug("download %s: renamed %s to %s", dl.id, part, final)
        if settings.mark_of_the_web:
            winutil.write_zone_identifier(
                str(final), net.safe_url(self._fetch_url or dl.url),
                net.safe_url(dl.referrer) if dl.referrer else "",
            )
        dl.completed_at = time.time()
        if settings.scan_after_download:
            clean, message = winutil.scan_with_defender(str(final))
            log.info("download %s: Defender says: %s", dl.id, message)
            if not clean:
                # The scan runs without remediation, so nothing else removes it.
                try:
                    final.unlink()
                except OSError:
                    raise DownloadError(f"{message}. Delete it without opening it: {final}") from None
                raise DownloadError(f"{message}, so Downpour deleted it")
        # Last: writing the Mark of the Web and scanning both touch the file's time.
        if settings.keep_server_date and dl.last_modified:
            if winutil.set_file_time(str(final), dl.last_modified):
                log.debug("download %s: file dated %s, as the server said", dl.id, dl.last_modified)
        if settings.after_download_command.strip():
            winutil.run_after_download(settings.after_download_command, str(final))
