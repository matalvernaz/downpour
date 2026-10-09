"""Application wiring: settings, engine, window, tray, browser requests,
clipboard watching and the scheduler."""

from __future__ import annotations

import collections
import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import wx

from downpour import APP_NAME, __version__
from downpour.core import filenames, ipc, winutil
from downpour.core import settings as settings_mod
from downpour.core.engine import Engine
from downpour.core.models import Download, Status
from downpour.core.paths import app_dir, data_dir
from downpour.core.store import Store
from downpour.core.units import format_size, format_speed, parse_size
from downpour.gui import forms
from downpour.gui.dialogs import (
    AddUrlDialog,
    BatchDialog,
    ChecksumDialog,
    CountdownDialog,
    DownloadInfoDialog,
    LinkPickerDialog,
    PropertiesDialog,
    clipboard_text,
    item_to_download,
    urls_in,
)
from downpour.gui.feedback import Feedback
from downpour.gui.main_frame import MainFrame
from downpour.gui.options_dialog import OptionsDialog
from downpour.gui.tray import TrayIcon, make_icon

log = logging.getLogger(__name__)

# Downloads the browser should hand over even when the name has no telling
# extension, judged by Content-Type.
CAPTURE_MIME_PREFIXES = ("video/", "audio/")
CAPTURE_MIMES = {
    "application/zip", "application/x-zip-compressed", "application/x-7z-compressed",
    "application/x-rar-compressed", "application/vnd.rar", "application/x-msdownload",
    "application/x-msi", "application/x-iso9660-image", "application/gzip",
    "application/x-tar", "application/vnd.android.package-archive", "application/pdf",
}


def bring_to_front(win: wx.TopLevelWindow) -> None:
    """Really take the foreground, so the screen reader announces the window.

    Requests from the browser host and the command line call
    AllowSetForegroundWindow first, which is what makes this permitted.
    """
    if not win:
        return
    if win.IsIconized():
        win.Iconize(False)
    win.Raise()
    if sys.platform == "win32":
        ctypes.windll.user32.SetForegroundWindow(win.GetHandle())


class Controller:
    def __init__(self, app: wx.App, settings: settings_mod.Settings, engine: Engine):
        self.app = app
        self.settings = settings
        self.engine = engine
        self.feedback = Feedback(settings)
        self.icon = make_icon()
        self.frame = MainFrame(self)
        self.frame.SetIcon(self.icon)
        self.tray = TrayIcon(self)
        self.feedback.use_taskbar_icon(self.tray)
        self._dialogs: collections.deque = collections.deque()
        self._dialog_busy = False
        self._exiting = False
        self.clipboard = ClipboardWatcher(self)
        self._schedule_marks: dict[str, str] = {}
        self.timer = wx.Timer()
        self.timer.Bind(wx.EVT_TIMER, self._tick)
        self.timer.Start(2000)
        engine.subscribe(self._on_engine_event)

    # -- engine events (any thread) -------------------------------------------

    def _on_engine_event(self, event: str, dl: Download | None) -> None:
        if event in ("completed", "failed", "idle", "added", "removed"):
            wx.CallAfter(self._handle_event, event, dl)

    def _handle_event(self, event: str, dl: Download | None) -> None:
        if self._exiting:
            return
        if event == "completed" and dl is not None:
            self.feedback.announce("Download complete", dl.display_name(), sound="complete",
                                   spoken=f"Download complete: {dl.display_name()}")
            if self.settings.show_complete_dialog:
                self._queue_dialog(lambda: self._complete_dialog(dl))
        elif event == "failed" and dl is not None:
            self.feedback.announce("Download failed", f"{dl.display_name()}: {dl.error}", sound="failed",
                                   spoken=f"Download failed: {dl.display_name()}. {dl.error}")
        elif event == "idle":
            self._queue_finished()
        self.frame.refresh()

    def _tick(self, evt) -> None:
        running, queued, speed = self.engine.totals()
        if running:
            tip = f"Downpour: {running} downloading at {format_speed(speed) or '0 bytes/s'}"
        elif queued:
            tip = f"Downpour: {queued} queued"
        else:
            tip = "Downpour"
        self.tray.set_tooltip(tip)
        self._check_schedule()

    # -- requests from the browser host and the command line (IPC thread) ------

    def handle_request(self, msg: dict) -> dict:
        kind = msg.get("type")
        if kind in ("hello", "ping"):
            return {"ok": True, "app": APP_NAME, "version": __version__,
                    "capture": self.settings.browser_capture, "pid": os.getpid()}
        if kind == "show":
            wx.CallAfter(self.show_window)
            return {"ok": True}
        if kind == "offer":
            item = msg.get("download")
            if not isinstance(item, dict) or not str(item.get("url", "")).lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "offer needs a download with an http or https url"}
            accept = self.wants(item)
            if accept:
                wx.CallAfter(self.offer, item)
            return {"ok": True, "accept": accept}
        if kind == "add":
            items = [i for i in msg.get("items") or [] if isinstance(i, dict) and i.get("url")]
            if not items:
                return {"ok": False, "error": "no links to add"}
            wx.CallAfter(self.add_items, items, bool(msg.get("pick")))
            return {"ok": True}
        if kind == "media":
            return {"ok": False, "error": "Grabbing video and audio from pages is not built yet."}
        return {"ok": False, "error": f"unknown request type {kind!r}"}

    def wants(self, item: dict) -> bool:
        """Should Downpour take this download from the browser?"""
        s = self.settings
        if not s.browser_capture:
            return False
        url = str(item.get("finalUrl") or item.get("url"))
        name = str(item.get("filename") or filenames.from_url(url))
        size = item.get("size") if isinstance(item.get("size"), int) and item["size"] >= 0 else None
        if size is not None and size < s.capture_min_size:
            return False
        if s.wants_capture(name, size):
            return True
        mime = str(item.get("mime") or "").split(";")[0].strip().lower()
        has_known_ext = bool(os.path.splitext(name)[1])
        return not has_known_ext and (mime.startswith(CAPTURE_MIME_PREFIXES) or mime in CAPTURE_MIMES)

    # -- windows ----------------------------------------------------------------

    def show_window(self) -> None:
        if self._exiting:
            return
        self.frame.Show()
        self.frame.refresh()
        bring_to_front(self.frame)
        self.frame.list.SetFocus()

    def _parent(self) -> wx.Window | None:
        return self.frame if self.frame.IsShown() else None

    def _queue_dialog(self, show) -> None:
        """New-download dialogs open one at a time, in arrival order."""
        self._dialogs.append(show)
        if not self._dialog_busy:
            wx.CallAfter(self._next_dialog)

    def _next_dialog(self) -> None:
        if self._dialog_busy or not self._dialogs or self._exiting:
            return
        show = self._dialogs.popleft()
        self._dialog_busy = True
        try:
            show()
        finally:
            self._dialog_busy = False
            if self._dialogs:
                wx.CallAfter(self._next_dialog)

    def _modal(self, dlg: wx.Dialog) -> int:
        wx.CallAfter(bring_to_front, dlg)
        return dlg.ShowModal()

    # -- adding downloads -------------------------------------------------------

    def add_url_dialog(self) -> None:
        def show():
            with AddUrlDialog(self._parent()) as dlg:
                if self._modal(dlg) != wx.ID_OK:
                    return
                urls = dlg.urls
            if len(urls) == 1:
                self._confirm_download(Download(url=urls[0]), interactive=True)
            else:
                self._pick([Download(url=u) for u in urls])

        self._queue_dialog(show)

    def add_from_clipboard(self) -> None:
        urls = urls_in(clipboard_text())
        if not urls:
            forms.message(self._parent(), "There are no links on the clipboard.")
            return
        self.add_urls(urls)

    def batch_dialog(self) -> None:
        def show():
            with BatchDialog(self._parent()) as dlg:
                if self._modal(dlg) != wx.ID_OK:
                    return
                urls = dlg.urls
            self._pick([Download(url=u) for u in urls], title=f"Batch download: {len(urls)} addresses")

        self._queue_dialog(show)

    def import_links(self) -> None:
        with wx.FileDialog(self._parent(), "Import links from a text file", wildcard="Text files (*.txt)|*.txt|All files|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            forms.message(self._parent(), f"Could not read the file: {e}", icon=wx.ICON_ERROR)
            return
        urls = urls_in(text)
        if not urls:
            forms.message(self._parent(), "That file has no http or https links in it.")
            return
        self._queue_dialog(lambda: self._pick([Download(url=u) for u in urls], title=f"Import: {len(urls)} links"))

    def add_urls(self, urls: list[str]) -> None:
        if len(urls) == 1:
            self.offer({"url": urls[0]}, interactive=True)
        elif urls:
            self._queue_dialog(lambda: self._pick([Download(url=u) for u in urls]))

    def add_items(self, items: list[dict], pick: bool) -> None:
        downloads = [item_to_download(i) for i in items]
        if len(downloads) == 1 and not pick:
            self.offer(items[0], interactive=True)
        else:
            self._queue_dialog(lambda: self._pick(downloads))

    def offer(self, item: dict, interactive: bool = False) -> None:
        """A single download arrived from the browser, the clipboard or the user."""
        dl = item_to_download(item)
        existing = self.engine.find_url(dl.url)
        if existing is not None and existing.status != Status.COMPLETED:
            self.engine.start([existing.id])
            self.feedback.say(f"Already in the list: {existing.display_name()}. Resuming.")
            return
        if interactive or self.settings.show_info_dialog:
            self._queue_dialog(lambda: self._confirm_download(dl, interactive=True))
        else:
            self.engine.add(dl, start=True, force=True)
            self.feedback.play("added")
            self.feedback.say(f"Downloading {dl.display_name()}")

    def _confirm_download(self, dl: Download, interactive: bool) -> None:
        with DownloadInfoDialog(self._parent(), self.settings, dl) as dlg:
            if self._modal(dlg) != wx.ID_OK:
                return
            choice = dlg.choice
        start = choice == DownloadInfoDialog.START
        try:
            self.engine.add(dl, start=start, force=start)
        except ValueError as e:
            forms.message(self._parent(), str(e), icon=wx.ICON_WARNING)
            return
        self.feedback.play("added")

    def _pick(self, downloads: list[Download], title: str = "Choose downloads") -> None:
        with LinkPickerDialog(self._parent(), self.settings, downloads, title=title) as dlg:
            if self._modal(dlg) != wx.ID_OK:
                return
            chosen, start = dlg.items, dlg.start_now
        added = 0
        for dl in chosen:
            try:
                self.engine.add(dl, start=start)
                added += 1
            except ValueError:
                continue
        self.feedback.play("added")
        self.feedback.say(f"{added} downloads added" + ("" if start else ", paused"))

    # -- other dialogs ----------------------------------------------------------

    def properties_dialog(self, dl: Download) -> None:
        with PropertiesDialog(self._parent(), dl, self.engine.is_running(dl.id)) as dlg:
            if self._modal(dlg) != wx.ID_OK:
                return
            values, new_url = dlg.values, dlg.new_url
        for key, value in values.items():
            setattr(dl, key, value)
        self.engine.update(dl)
        if new_url != dl.url:
            self.engine.refresh_url(dl.id, new_url)
            self.feedback.say("Address changed")

    def checksum_dialog(self, dl: Download) -> None:
        if dl.status != Status.COMPLETED or not os.path.exists(dl.path):
            forms.message(self._parent(), "Checksums are available once the file has finished downloading.")
            return
        with ChecksumDialog(self._parent(), dl.path, dl.checksum) as dlg:
            self._modal(dlg)

    def options_dialog(self, page: int = 0) -> None:
        with OptionsDialog(self._parent(), self.settings, page=page) as dlg:
            if self._modal(dlg) != wx.ID_OK:
                return
        self.apply_settings()

    def apply_settings(self) -> None:
        settings_mod.save(self.settings)
        self.engine.apply_settings()
        try:
            winutil.set_start_with_windows(self.settings.start_with_windows, autostart_command())
        except OSError as e:
            log.warning("could not update the startup entry: %s", e)
        self.frame.rebuild_filters()

    def _complete_dialog(self, dl: Download) -> None:
        msg = f"{dl.display_name()} finished downloading ({format_size(dl.size)}).\n\nSaved in {dl.directory}"
        with wx.MessageDialog(self._parent(), msg, "Download complete", wx.YES_NO | wx.CANCEL | wx.ICON_INFORMATION) as dlg:
            dlg.SetYesNoCancelLabels("&Open", "Open &folder", "&Close")
            answer = self._modal(dlg)
        if answer == wx.ID_YES:
            self.open_download(dl)
        elif answer == wx.ID_NO:
            self.open_folder(dl)

    def about(self) -> None:
        forms.message(
            self._parent(),
            f"{APP_NAME} {__version__}\n\nA free, open-source download manager built for screen-reader users.\n"
            "https://github.com/matalvernaz/downpour",
            title=f"About {APP_NAME}",
        )

    # -- small actions ------------------------------------------------------------

    def open_download(self, dl: Download) -> None:
        if dl.status != Status.COMPLETED:
            self.feedback.say("Not finished yet")
            return
        if not os.path.exists(dl.path):
            forms.message(self._parent(), f"The file is no longer at {dl.path}.", icon=wx.ICON_WARNING)
            return
        try:
            winutil.open_path(dl.path)
        except OSError as e:
            forms.message(self._parent(), f"Windows could not open the file: {e}", icon=wx.ICON_ERROR)

    def open_folder(self, dl: Download) -> None:
        target = dl.path if dl.status == Status.COMPLETED else dl.part_path
        if target and os.path.exists(target):
            winutil.reveal(target)
        elif dl.directory and os.path.isdir(dl.directory):
            winutil.open_path(dl.directory)
        else:
            forms.message(self._parent(), "That folder does not exist yet.")

    def copy_text(self, text: str) -> None:
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
            self.clipboard.ignore_current()

    def set_speed_limit(self, rate: int) -> None:
        if rate:
            self.settings.speed_limit = rate
        self.settings.speed_limit_enabled = bool(rate)
        self.apply_settings()
        self.feedback.say(f"Speed limited to {format_speed(rate)}" if rate else "No speed limit")

    def custom_speed_limit(self) -> None:
        current = format_size(self.settings.speed_limit).replace(" ", "") if self.settings.speed_limit else "1MB"
        with wx.TextEntryDialog(self._parent(), "Limit the total speed to (for example 500K or 2M):", "Speed limit", current) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            rate = parse_size(dlg.GetValue())
        if not rate:
            forms.message(self._parent(), "Write the speed like 500K or 2M.", icon=wx.ICON_WARNING)
            return
        self.set_speed_limit(rate)

    def toggle_clipboard(self) -> None:
        self.settings.clipboard_monitor = not self.settings.clipboard_monitor
        self.apply_settings()
        self.feedback.say("Watching the clipboard" if self.settings.clipboard_monitor else "Not watching the clipboard")

    def set_when_done(self, key: str) -> None:
        self.settings.when_done = key
        self.apply_settings()

    # -- scheduler and queue end ------------------------------------------------------

    def _check_schedule(self) -> None:
        s = self.settings
        if not s.schedule_enabled:
            return
        now = time.localtime()
        if now.tm_wday not in s.schedule_days:
            return
        clock = time.strftime("%H:%M", now)
        today = time.strftime("%Y-%m-%d", now)
        for mark, at, action in (("start", s.schedule_start, self.engine.start_all), ("stop", s.schedule_stop, self.engine.pause_all)):
            if at and _normal_time(at) == clock and self._schedule_marks.get(mark) != today:
                self._schedule_marks[mark] = today
                log.info("scheduler: %s at %s", mark, clock)
                action()

    def _queue_finished(self) -> None:
        action = self.settings.when_done
        if action == "nothing":
            return
        self.feedback.announce("All downloads finished", "", sound="queue-done", spoken="All downloads finished")

        def show():
            with CountdownDialog(self._parent(), action) as dlg:
                if self._modal(dlg) != wx.ID_OK:
                    return
            self.settings.when_done = "nothing"  # one-shot, like IDM
            settings_mod.save(self.settings)
            if action == "exit":
                self.exit()
            else:
                self.exit(then=lambda: winutil.power_action(action))

        self._queue_dialog(show)

    # -- shutdown --------------------------------------------------------------------

    def exit(self, then=None) -> None:
        if self._exiting:
            return
        self._exiting = True
        self._after_exit = then
        self.timer.Stop()
        self.clipboard.stop()
        self.frame.exiting = True
        self.frame.timer.Stop()
        self.tray.RemoveIcon()
        self.tray.Destroy()
        self.frame.Destroy()


def _normal_time(text: str) -> str:
    try:
        h, m = text.strip().split(":")
        return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        return ""


class ClipboardWatcher:
    """Offers a single copied link to a downloadable file, like IDM does.

    Polls the clipboard's sequence number, which costs nothing, and reads the
    text only when it changed. Links Downpour copied itself are ignored.
    """

    def __init__(self, controller: Controller):
        self.ctl = controller
        self._seq = self._ignore = self._sequence()
        self._last_url = ""
        self.timer = wx.Timer()
        self.timer.Bind(wx.EVT_TIMER, self._poll)
        self.timer.Start(800)

    @staticmethod
    def _sequence() -> int:
        if sys.platform != "win32":
            return 0
        return ctypes.windll.user32.GetClipboardSequenceNumber()

    def ignore_current(self) -> None:
        self._ignore = self._sequence()

    def stop(self) -> None:
        self.timer.Stop()

    def _poll(self, evt) -> None:
        seq = self._sequence()
        if seq == self._seq:
            return
        self._seq = seq
        if seq == self._ignore or not self.ctl.settings.clipboard_monitor:
            return
        text = clipboard_text().strip()
        if not text or len(text) > 4096:
            return
        urls = urls_in(text)
        if len(urls) != 1 or urls[0] == self._last_url or len(text) > len(urls[0]) + 2:
            return  # only a bare link counts; a pasted paragraph is not a request
        url = urls[0]
        name = filenames.from_url(url)
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        if ext not in self.ctl.settings.capture_extensions:
            return
        self._last_url = url
        self.ctl.offer({"url": url})


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    runner = windowless if windowless.exists() else exe
    root = str(app_dir())
    code = f"import sys; sys.path.insert(0, r'{root}'); from downpour.__main__ import main; sys.exit(main(['downpour', '--tray']))"
    return f'"{runner}" -c "{code}"'


def _setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(data_dir() / "downpour.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def excepthook(kind, value, tb):
        log.error("unhandled error:\n%s", "".join(traceback.format_exception(kind, value, tb)))
        try:
            wx.MessageBox(f"Something went wrong: {value}\n\nDetails are in {data_dir() / 'downpour.log'}",
                          "Downpour error", wx.OK | wx.ICON_ERROR)
        except Exception:  # noqa: BLE001 - never let the error handler fail
            pass

    sys.excepthook = excepthook
    threading.excepthook = lambda args: log.error(
        "unhandled error in thread %s:\n%s", args.thread.name if args.thread else "?",
        "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
    )


def run(urls: list[str] | None = None, tray: bool = False) -> int:
    _setup_logging()
    log.info("starting %s %s", APP_NAME, __version__)
    settings = settings_mod.load()
    ready = threading.Event()
    holder: dict[str, Controller] = {}

    def handler(msg: dict) -> dict:
        if not ready.wait(30):
            return {"ok": False, "error": "Downpour is still starting"}
        return holder["ctl"].handle_request(msg)

    try:
        server = ipc.Server(handler)
    except ipc.AlreadyRunning:
        message = {"type": "add", "items": [{"url": u} for u in urls], "pick": len(urls) > 1} if urls else {"type": "show"}
        try:
            ipc.request(message)
        except (ipc.NotRunning, OSError, TimeoutError):
            pass
        return 0

    app = wx.App(False)
    app.SetAppName(APP_NAME)
    engine = Engine(Store(data_dir() / "downpour.db"), settings)
    ctl = Controller(app, settings, engine)
    holder["ctl"] = ctl
    server.start()
    ready.set()
    engine.begin()
    if not tray:
        ctl.show_window()
    if urls:
        wx.CallAfter(ctl.add_urls, urls)
    app.MainLoop()
    server.close()
    engine.shutdown()
    after = getattr(ctl, "_after_exit", None)
    if after is not None:
        after()
    log.info("stopped")
    return 0
