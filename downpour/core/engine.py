"""The download manager proper: every download, the queue, and the tasks.

All public methods are thread-safe and return quickly; the slow work happens
on task threads. Observers subscribe with ``subscribe(fn)`` and receive
``fn(event, download)`` calls from whichever thread caused the change, so a
GUI must marshal them onto its own thread.

Events: ``added``, ``removed``, ``changed`` (status or details), ``completed``,
``failed``, and ``idle`` (download is None) when the last running download
finishes and nothing is left in the queue.

Live progress figures (bytes, speed, time left) change constantly and are
not announced; read them from the Download objects returned by
``downloads()``, which are the live records. Treat them as read-only.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

from downpour.core import filenames
from downpour.core.limiter import RateLimiter
from downpour.core.models import ACTIVE, Download, Status
from downpour.core.settings import Settings
from downpour.core.store import Store
from downpour.core.task import HttpTask, Outcome

log = logging.getLogger(__name__)

Listener = Callable[[str, Download | None], None]
SCHEMES = ("http", "https")


def make_task(engine: "Engine", dl: Download):
    if dl.kind == "media":
        from downpour.core.media import MediaTask

        return MediaTask(engine, dl)
    return HttpTask(engine, dl)


class Engine:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.limiter = RateLimiter(settings.effective_speed_limit())
        self._lock = threading.RLock()
        self._downloads: dict[int, Download] = {}
        self._tasks: dict[int, object] = {}
        self._after_stop: dict[int, tuple] = {}  # what to do when a stopping task ends
        self._forced: set[int] = set()  # start even if the queue is full
        self._listeners: list[Listener] = []
        self._closed = False
        self._busy = False
        for dl in store.load_all():
            if dl.status in ACTIVE:
                dl.status = Status.QUEUED  # running when the app last closed: carry on
            self._downloads[dl.id] = dl

    def begin(self) -> None:
        """Start working through the queue, including downloads that were
        running when the app last closed. Subscribe observers first."""
        self._schedule()

    # -- observers ------------------------------------------------------------

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def unsubscribe(self, fn: Listener) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def _emit(self, event: str, dl: Download | None = None) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, dl)
            except Exception:  # noqa: BLE001 - a broken observer must not stop downloads
                log.exception("event listener failed")

    # -- queries --------------------------------------------------------------

    def downloads(self) -> list[Download]:
        with self._lock:
            return sorted(self._downloads.values(), key=lambda d: (d.position, d.id))

    def get(self, download_id: int) -> Download | None:
        return self._downloads.get(download_id)

    def find_url(self, url: str) -> Download | None:
        with self._lock:
            for dl in self._downloads.values():
                if dl.url == url:
                    return dl
        return None

    def is_running(self, download_id: int) -> bool:
        return download_id in self._tasks

    def totals(self) -> tuple[int, int, float]:
        """(downloads running, downloads queued, combined speed in bytes/s)."""
        with self._lock:
            running = len(self._tasks)
            queued = sum(1 for d in self._downloads.values() if d.status == Status.QUEUED and d.id not in self._tasks)
            speed = sum(d.speed for d in self._downloads.values() if d.id in self._tasks)
        return running, queued, speed

    # -- commands -------------------------------------------------------------

    def add(self, dl: Download, start: bool = True, force: bool = False) -> Download:
        """Add a download. ``force`` starts it now even if the queue is full,
        which is what the user expects from "Start download"."""
        if urlsplit(dl.url).scheme.lower() not in SCHEMES:
            raise ValueError(f"Only http and https addresses can be downloaded: {dl.url}")
        with self._lock:
            dl.status = Status.QUEUED if start else Status.PAUSED
            dl.position = max((d.position for d in self._downloads.values()), default=0.0) + 1
            if dl.kind == "media":
                # Without a name, yt-dlp names the file after the video's title.
                if dl.filename and dl.directory:
                    dl.filename = filenames.unique_stem(Path(dl.directory), dl.filename, self._media_stems(dl))
            elif not dl.filename:
                dl.filename = filenames.sanitize(filenames.from_url(dl.url))
            self.store.insert(dl)
            self._downloads[dl.id] = dl
            if start and force:
                self._forced.add(dl.id)
        self._emit("added", dl)
        self._schedule()
        return dl

    def _media_stems(self, dl: Download) -> set[str]:
        """Names that other unfinished video and audio downloads into the same
        folder will use, without extensions (yt-dlp picks those at the end)."""
        folder = os.path.normcase(os.path.abspath(dl.directory))
        return {
            os.path.splitext(d.filename)[0].lower()
            for d in self._downloads.values()
            if d.kind == "media" and d.filename and d.status != Status.COMPLETED
            and os.path.normcase(os.path.abspath(d.directory or ".")) == folder
        }

    def start(self, ids: Iterable[int], force: bool = True) -> None:
        changed = []
        with self._lock:
            for i in ids:
                dl = self._downloads.get(i)
                if dl is None or dl.status == Status.COMPLETED:
                    continue
                task = self._tasks.get(i)
                if task is not None:
                    if task.stopping and i not in self._after_stop:
                        self._after_stop[i] = ("restart",)  # still winding down; go again after
                    continue
                if dl.status in (Status.PAUSED, Status.FAILED, Status.QUEUED):
                    dl.status = Status.QUEUED
                    dl.error = ""
                    if force:
                        self._forced.add(i)
                    self.save(dl)
                    changed.append(dl)
        for dl in changed:
            self._emit("changed", dl)
        self._schedule()

    def pause(self, ids: Iterable[int]) -> None:
        changed = []
        with self._lock:
            for i in ids:
                dl = self._downloads.get(i)
                if dl is None:
                    continue
                self._forced.discard(i)
                task = self._tasks.get(i)
                if task is not None:
                    # Pausing overrides a pending restart, never a removal or redownload.
                    if self._after_stop.get(i, ("",))[0] == "restart":
                        del self._after_stop[i]
                    task.stop(Status.PAUSED)
                elif dl.status == Status.QUEUED:
                    dl.status = Status.PAUSED
                    self.save(dl)
                    changed.append(dl)
        for dl in changed:
            self._emit("changed", dl)
        self._schedule()

    def start_all(self) -> None:
        with self._lock:
            ids = [d.id for d in self._downloads.values() if d.status in (Status.PAUSED, Status.FAILED)]
        self.start(ids, force=False)

    def pause_all(self) -> None:
        with self._lock:
            ids = [d.id for d in self._downloads.values() if d.status in ACTIVE or d.status == Status.QUEUED]
        self.pause(ids)

    def remove(self, ids: Iterable[int], delete_files: bool = False) -> None:
        gone = []
        with self._lock:
            for i in ids:
                dl = self._downloads.get(i)
                if dl is None:
                    continue
                task = self._tasks.get(i)
                if task is not None:
                    self._after_stop[i] = ("remove", delete_files)
                    task.stop(Status.PAUSED)
                else:
                    gone.append(dl)
        for dl in gone:
            self._finish_remove(dl, delete_files)

    def _finish_remove(self, dl: Download, delete_files: bool) -> None:
        with self._lock:
            self._downloads.pop(dl.id, None)
            self._forced.discard(dl.id)
        self.store.delete([dl.id])
        if dl.part_path:
            _unlink(dl.part_path)
        if delete_files and dl.status == Status.COMPLETED and dl.path:
            _unlink(dl.path)
        self._emit("removed", dl)
        self._schedule()

    def redownload(self, download_id: int) -> None:
        """Throw away what was downloaded (including a finished file) and
        fetch it again from scratch."""
        with self._lock:
            dl = self._downloads.get(download_id)
            if dl is None:
                return
            task = self._tasks.get(download_id)
            if task is not None:
                self._after_stop[download_id] = ("redownload",)
                task.stop(Status.PAUSED)
                return
        self._reset(dl)

    def _reset(self, dl: Download) -> None:
        if dl.part_path:
            _unlink(dl.part_path)
        if dl.status == Status.COMPLETED and dl.path:
            _unlink(dl.path)
        with self._lock:
            dl.segments = []
            dl.downloaded = 0
            dl.size = None
            dl.resumable = None
            dl.checksum_ok = None
            dl.completed_at = None
            dl.error = ""
            dl.status = Status.QUEUED
            self._forced.add(dl.id)
            self.save(dl)
        self._emit("changed", dl)
        self._schedule()

    def refresh_url(self, download_id: int, url: str) -> None:
        """Point a download at a new address (an expired link renewed), keeping
        what is already downloaded."""
        if urlsplit(url).scheme.lower() not in SCHEMES:
            raise ValueError("Only http and https addresses can be downloaded")
        with self._lock:
            dl = self._downloads.get(download_id)
            if dl is None:
                return
            dl.url = url
            # A renewed link may come from another server whose ETag differs
            # for the same file; resume on the size check alone this once.
            dl.etag = dl.last_modified = ""
            self.save(dl)
            task = self._tasks.get(download_id)
            if task is not None:
                self._after_stop[download_id] = ("restart",)
                task.stop(Status.PAUSED)
        self._emit("changed", dl)

    def update(self, dl: Download) -> None:
        """Persist edits the GUI made to a download's details."""
        with self._lock:
            self.save(dl)
            task = self._tasks.get(dl.id)
        if task is not None:
            task.set_speed_limit(dl.speed_limit)
        self._emit("changed", dl)

    def move(self, ids: Iterable[int], offset: int) -> None:
        """Move downloads up (negative) or down the queue."""
        ids = set(ids)
        with self._lock:
            order = self.downloads()
            if offset < 0:
                for idx in range(1, len(order)):
                    if order[idx].id in ids and order[idx - 1].id not in ids:
                        order[idx - 1], order[idx] = order[idx], order[idx - 1]
            elif offset > 0:
                for idx in range(len(order) - 2, -1, -1):
                    if order[idx].id in ids and order[idx + 1].id not in ids:
                        order[idx + 1], order[idx] = order[idx], order[idx + 1]
            for pos, dl in enumerate(order, start=1):
                if dl.position != pos:
                    dl.position = float(pos)
                    self.save(dl)
        self._emit("changed", None)

    def apply_settings(self) -> None:
        self.limiter.set_rate(self.settings.effective_speed_limit())
        self._schedule()

    # -- callbacks from tasks -------------------------------------------------

    def _set_status(self, dl: Download, status: Status) -> None:
        dl.status = status
        self._emit("changed", dl)

    def save(self, dl: Download, segments: list | None = None) -> None:
        """Persist ``dl``. While a task runs, its live segments may be ahead of
        what has been fsynced, so the task's last checkpoint is saved instead."""
        if segments is None:
            task = self._tasks.get(dl.id)
            segments = getattr(task, "checkpointed", None) if task is not None else None
        self.store.update(dl, segments)

    def claim_path(self, dl: Download, path: Path) -> Path:
        """Reserve a final file name for ``dl``, avoiding every other download's
        name and existing files (unless the user prefers overwriting)."""
        with self._lock:
            taken = {
                d.path.lower()
                for d in self._downloads.values()
                if d.id != dl.id and d.path and d.status != Status.COMPLETED
            }
            key = str(path).lower()
            own = (dl.path or "").lower()
            if key == own and not path.exists() and key not in taken:
                return path  # our own name from an earlier attempt
            if (
                self.settings.duplicate_action == "overwrite"
                and key not in taken
                and not Path(str(path) + ".part").exists()
            ):
                return path
            return filenames.unique_path(path, taken)

    def _task_done(self, task, outcome: Outcome, error: str) -> None:
        dl = task.dl
        with self._lock:
            if self._tasks.get(dl.id) is task:
                del self._tasks[dl.id]
            self._forced.discard(dl.id)
            after = self._after_stop.pop(dl.id, ("",))
            if outcome is Outcome.COMPLETED:
                dl.status, dl.error = Status.COMPLETED, ""
            elif outcome is Outcome.FAILED:
                dl.status, dl.error = Status.FAILED, error
            else:
                dl.status = task.stop_status
            if after[0] == "restart" and dl.status != Status.COMPLETED:
                dl.status, dl.error = Status.QUEUED, ""
                self._forced.add(dl.id)
            if after[0] != "remove":
                self.store.update(dl)
        if after[0] == "remove":
            self._finish_remove(dl, after[1])
        elif after[0] == "redownload":
            self._reset(dl)
        else:
            event = {Outcome.COMPLETED: "completed", Outcome.FAILED: "failed"}.get(outcome, "changed")
            self._emit(event, dl)
        self._schedule()

    # -- queue ----------------------------------------------------------------

    def _schedule(self) -> None:
        started = []
        with self._lock:
            if self._closed:
                return
            running = len(self._tasks)
            for dl in self.downloads():
                if dl.status != Status.QUEUED or dl.id in self._tasks:
                    continue
                if running >= self.settings.max_active and dl.id not in self._forced:
                    continue
                task = make_task(self, dl)
                self._tasks[dl.id] = task
                running += 1
                started.append(task)
            busy = bool(self._tasks) or any(d.status == Status.QUEUED for d in self._downloads.values())
            went_idle = self._busy and not busy
            self._busy = busy
        for task in started:
            task.start()
        if went_idle:
            self._emit("idle", None)

    def shutdown(self, timeout: float = 20.0) -> None:
        """Stop every task, leaving running downloads queued for next launch."""
        with self._lock:
            self._closed = True
            tasks = list(self._tasks.values())
        for task in tasks:
            task.stop(Status.QUEUED)
        for task in tasks:
            task.thread.join(timeout)
        self.store.close()


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not delete %s", path, exc_info=True)
