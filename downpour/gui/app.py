"""Application wiring: settings, engine, window, tray, browser requests,
clipboard watching and the scheduler."""

from __future__ import annotations

import collections
import ctypes
import logging
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import wx

from downpour import APP_NAME, __version__
from downpour.core import filenames, ipc, logs, updates, winutil
from downpour.core import settings as settings_mod
from downpour.core.engine import Engine
from downpour.core.logs import redact_url, summarize_cookies
from downpour.core.models import Download, Status
from downpour.core.paths import data_dir, logs_dir
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
    StreamPickerDialog,
    clipboard_text,
    item_to_download,
    urls_in,
)
from downpour.core import media
from downpour.gui.feedback import Feedback
from downpour.gui.main_frame import MainFrame
from downpour.gui.media_dialog import MediaDialog, media_format_value, run_with_progress
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
    def __init__(self, app: wx.App, settings: settings_mod.Settings, engine: Engine, debug: bool = False):
        self.app = app
        self.settings = settings
        self.engine = engine
        self.debug = debug  # --debug or DOWNPOUR_DEBUG: detailed logs whatever the settings say
        self._applied = asdict(settings)
        self.feedback = Feedback(settings)
        self.icon = make_icon()
        self.frame = MainFrame(self)
        self.frame.SetIcon(self.icon)
        self.tray = TrayIcon(self)
        self.feedback.use_taskbar_icon(self.tray)
        self._dialogs: collections.deque = collections.deque()
        self._dialog_busy = False
        self._exiting = False
        self._awaiting_refresh: int | None = None  # download waiting for a fresh link
        self._handed_back: dict[str, float] = {}  # links given back to the browser, with expiry
        self._pass_next = 0.0  # until when the next offered download stays in the browser
        self.clipboard = ClipboardWatcher(self)
        self._schedule_marks: dict[str, str] = {}
        self.timer = wx.Timer()
        self.timer.Bind(wx.EVT_TIMER, self._tick)
        self.timer.Start(2000)
        self._started = time.monotonic()
        self._update_checking = False
        engine.subscribe(self._on_engine_event)

    # -- engine events (any thread) -------------------------------------------

    def _on_engine_event(self, event: str, dl: Download | None) -> None:
        if event == "added" and dl is not None and dl.directory:
            settings_mod.remember_folder(self.settings, dl.directory)
            settings_mod.save(self.settings)
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
        winutil.keep_awake(bool(running) and self.settings.keep_awake)
        self.engine.tick()
        if running:
            tip = f"Downpour: {running} downloading at {format_speed(speed) or '0 bytes/s'}"
        elif queued:
            tip = f"Downpour: {queued} queued"
        else:
            tip = "Downpour"
        self.tray.set_tooltip(tip)
        self._check_schedule()
        self._maybe_check_updates()

    # -- requests from the browser host and the command line (IPC thread) ------

    def handle_request(self, msg: dict) -> dict:
        kind = msg.get("type")
        if kind in ("hello", "ping"):
            log.debug("request %s: answering with version %s", kind, __version__)
            return {"ok": True, "app": APP_NAME, "version": __version__,
                    "capture": self.settings.browser_capture, "pid": os.getpid()}
        if kind == "show":
            log.info("request show: bringing the window forward")
            wx.CallAfter(self.show_window)
            return {"ok": True}
        if kind == "pass":
            # The extension's "let the browser keep the next download" key.
            self._pass_next = time.time() + 600
            log.info("request pass: the next browser download stays in the browser")
            wx.CallAfter(self.feedback.say, "The next download stays in the browser")
            return {"ok": True}
        if kind == "quit":
            log.info("request quit: closing, as an installer or the command line asked")
            wx.CallAfter(self.exit)
            return {"ok": True, "pid": os.getpid()}
        if kind == "offer":
            item = msg.get("download")
            if not isinstance(item, dict) or not str(item.get("url", "")).lower().startswith(("http://", "https://")):
                log.warning("request offer without a usable address; refused")
                return {"ok": False, "error": "offer needs a download with an http or https url"}
            accept = self.wants(item)
            if accept:
                wx.CallAfter(self.offer, item)
            return {"ok": True, "accept": accept}
        if kind == "add":
            items = [i for i in msg.get("items") or [] if isinstance(i, dict) and i.get("url")]
            if not items:
                log.warning("request add without links; refused")
                return {"ok": False, "error": "no links to add"}
            log.info(
                "request add: %d links from %s%s", len(items), redact_url(str(items[0].get("pageUrl") or "")) or "no page",
                ", to choose from" if msg.get("pick") else "",
            )
            wx.CallAfter(self.add_items, items, bool(msg.get("pick")))
            return {"ok": True}
        if kind == "media":
            page = msg.get("page")
            if not isinstance(page, dict) or not str(page.get("url", "")).lower().startswith(("http://", "https://")):
                log.warning("request media without a usable page address; refused")
                return {"ok": False, "error": "media needs a page with an http or https url"}
            cookies = [c for c in page.get("cookies") or [] if isinstance(c, dict)]
            found = media.found_streams(msg.get("found"))
            log.info("request media: grab from %s with %s; the page played %d streams", redact_url(str(page["url"])),
                     summarize_cookies(cookies), len(found))
            for stream in found:
                log.info("  played: %s, %s", stream.label(), redact_url(stream.url))
            wx.CallAfter(self.grab_media, str(page["url"]), cookies, str(page.get("userAgent") or ""),
                         str(page.get("referrer") or ""), found, str(page.get("pageTitle") or ""))
            return {"ok": True}
        log.warning("unknown request type %r", kind)
        return {"ok": False, "error": f"unknown request type {kind!r}"}

    def wants(self, item: dict) -> bool:
        """Should Downpour take this download from the browser? The answer
        and the reason for it go in the log: "why did it not capture my
        download?" is the commonest question there is."""
        accept, reason = self._capture_decision(item)
        url = str(item.get("finalUrl") or item.get("url") or "")
        size = item.get("size") if isinstance(item.get("size"), int) and item["size"] >= 0 else None
        log.info(
            "browser offered %s (%s, %s, %s) from %s: %s, %s",
            item.get("filename") or filenames.from_url(url), f"{size} bytes" if size is not None else "size unknown",
            item.get("mime") or "no type", summarize_cookies(item.get("cookies") if isinstance(item.get("cookies"), list) else None),
            redact_url(url), "taking it" if accept else "leaving it to the browser", reason,
        )
        return accept

    def _capture_decision(self, item: dict) -> tuple[bool, str]:
        s = self.settings
        now = time.time()
        for url in [str(item.get("url") or ""), str(item.get("finalUrl") or "")]:
            if url and self._handed_back.get(url, 0) > now:
                return False, "the user sent this one back to the browser"
        if self._pass_next > now:
            self._pass_next = 0.0
            return False, "the user asked to let the browser keep the next download"
        if self._awaiting_refresh is not None and self._refresh_match(item_to_download(item)) is not None:
            return True, "it renews the address of an unfinished download"
        if not s.browser_capture:
            return False, "taking over browser downloads is turned off in Options"
        url = str(item.get("finalUrl") or item.get("url"))
        name = str(item.get("filename") or filenames.from_url(url))
        size = item.get("size") if isinstance(item.get("size"), int) and item["size"] >= 0 else None
        if size is not None and size < s.capture_min_size:
            return False, f"it is smaller than the {s.capture_min_size}-byte minimum"
        if s.wants_capture(name, size):
            return True, f"{os.path.splitext(name)[1] or 'its type'} is in the file types to take over"
        mime = str(item.get("mime") or "").split(";")[0].strip().lower()
        has_known_ext = bool(os.path.splitext(name)[1])
        if not has_known_ext and (mime.startswith(CAPTURE_MIME_PREFIXES) or mime in CAPTURE_MIMES):
            return True, f"it has no file extension and its type {mime} is a download type"
        ext = os.path.splitext(name)[1]
        return False, (f"{ext} is not in the file types to take over" if ext
                       else f"it has no file extension and its type {mime or '(none)'} is not a download type")

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
        log.debug("dialog opened: %s", dlg.GetTitle())
        result = dlg.ShowModal()
        log.debug("dialog closed: %s (%s)", dlg.GetTitle(), {wx.ID_OK: "OK", wx.ID_CANCEL: "cancelled"}.get(result, result))
        return result

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

    def export_links(self, rows: list[Download]) -> None:
        """Save the addresses of the downloads now shown, one per line."""
        if not rows:
            forms.message(self._parent(), "There are no downloads in this view to export.")
            return
        with wx.FileDialog(self._parent(), "Export addresses", defaultFile="downloads.txt",
                           wildcard="Text files (*.txt)|*.txt", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            Path(path).write_text("\n".join(d.url for d in rows) + "\n", encoding="utf-8")
        except OSError as e:
            forms.message(self._parent(), f"Could not save the file: {e}", icon=wx.ICON_ERROR)
            return
        self.feedback.say(f"{len(rows)} addresses exported")

    def hand_to_browser(self, dl: Download) -> None:
        """Let the browser download it: for sites whose bot checks refuse
        anything that is not a browser. The extension offers the download
        to Downpour again, which declines it this time."""
        import webbrowser

        log.info("download %s: handing it to the browser for ten minutes", dl.id)
        self._handed_back[dl.url] = time.time() + 600
        self.engine.pause([dl.id])
        webbrowser.open(dl.url)
        self.feedback.say(f"Opening {dl.display_name()} in your browser")

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

    def wait_for_new_address(self, dl: Download) -> None:
        """IDM's "refresh download address": the next matching download the
        browser hands over renews this one's link instead of starting again."""
        log.info("download %s: waiting for a fresh address from the browser", dl.id)
        self._awaiting_refresh = dl.id
        self.engine.pause([dl.id])
        forms.message(
            self._parent(),
            f"Now go to the page {dl.display_name()} came from and start the download again in your browser. "
            "Downpour will take the new address and resume where it stopped.",
            title="Refresh address",
        )

    def _refresh_match(self, fresh: Download) -> Download | None:
        target = self.engine.get(self._awaiting_refresh) if self._awaiting_refresh else None
        if target is None:
            return None
        same_name = fresh.filename and fresh.filename.lower() == (target.filename or "").lower()
        same_size = fresh.size is not None and fresh.size == target.size
        return target if (same_name or same_size) else None

    def offer(self, item: dict, interactive: bool = False) -> None:
        """A single download arrived from the browser, the clipboard or the user."""
        dl = item_to_download(item)
        target = self._refresh_match(dl)
        if target is not None:
            log.info("download %s: received its fresh address", target.id)
            self._awaiting_refresh = None
            target.cookies = dl.cookies or target.cookies
            target.referrer = dl.referrer or target.referrer
            self.engine.refresh_url(target.id, dl.url)
            self.engine.start([target.id])
            self.feedback.say(f"New address received. Resuming {target.display_name()}")
            return
        existing = self.engine.find_url(dl.url)
        if existing is not None and existing.status != Status.COMPLETED:
            log.info("download %s already has this address; resuming it instead of adding another", existing.id)
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
        log.info("new download confirmed: %s, %s", dl.display_name(), "start now" if start else "later")
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

    # -- video and audio ----------------------------------------------------------

    def grab_media(self, url: str | None = None, cookies: list[dict] | None = None,
                   user_agent: str = "", referrer: str = "", found: list | None = None, title: str = "") -> None:
        def show():
            page = url
            if page is None:
                with AddUrlDialog(self._parent(), title="Grab video or audio", label="Page &address:") as dlg:
                    if self._modal(dlg) != wx.ID_OK:
                        return
                    page = dlg.urls[0]
            if found:
                with StreamPickerDialog(self._parent(), found, self.settings.recent_folders) as dlg:
                    if self._modal(dlg) != wx.ID_OK:
                        log.info("grab cancelled at the list of streams")
                        return
                    chosen, start, folder, use_ytdlp = dlg.chosen, dlg.start_now, dlg.folder_path, dlg.use_ytdlp
                if not use_ytdlp:
                    self._add_streams(chosen, page, title, user_agent, start, folder)
                    return
                log.info("grab: the user chose yt-dlp on the page instead of the streams it played")
            self._grab_with_ytdlp(page, cookies, user_agent, referrer)

        self._queue_dialog(show)

    def _add_streams(self, chosen: list, page: str, title: str, user_agent: str, start: bool, folder: str) -> None:
        added = 0
        for dl in media.stream_downloads(chosen, page, title, user_agent, self.settings.media_format):
            if folder:
                dl.directory = folder
            try:
                self.engine.add(dl, start=start, force=start)
            except ValueError as e:
                log.warning("could not add the stream %s: %s", redact_url(dl.url), e)
                continue
            added += 1
        log.info("grab: added %d of %d chosen streams%s", added, len(chosen), "" if start else ", paused")
        if added:
            self.feedback.play("added")
            self.feedback.say(f"{added} {'stream' if added == 1 else 'streams'} added" + ("" if start else ", paused"))

    def _grab_with_ytdlp(self, page: str, cookies: list[dict] | None, user_agent: str, referrer: str) -> None:
        if not self.ensure_ytdlp():
            return
        with MediaDialog(self._parent(), self.settings, page, cookies, user_agent, referrer) as dlg:
            if self._modal(dlg) != wx.ID_OK:
                return
            chosen, filename, directory = dlg.chosen, dlg.filename, dlg.directory
            start, playlist, info = dlg.start_now, dlg.playlist, dlg.info
            options = dlg.media_options
        needs_ffmpeg = chosen.needs_ffmpeg or options.get("sponsorblock") or options.get("subtitles") in ("embed", "text") \
            or options.get("clip_start") or options.get("clip_end") or options.get("audio_bitrate")
        if needs_ffmpeg and not media.find_ffmpeg(self.settings):
            self.ensure_ffmpeg(optional=True)
        if playlist and info is not None:
            if chosen.key.startswith("id:"):
                chosen = media.PRESETS[0]  # format ids differ from item to item
            items = [(u, media.suggested_name(media.MediaInfo(url=u, title=t, ext=info.ext), chosen), t)
                     for u, t in info.entries]
        else:
            items = [(page, filename, info.title if info else "")]
        added = 0
        for item_url, name, title in items:
            dl = Download(
                url=item_url, kind="media", media_format=media_format_value(chosen), media_options=dict(options),
                filename=name, filename_fixed=True, directory=directory, description=title,
                cookies=cookies or [], user_agent=user_agent, referrer=referrer,
            )
            try:
                self.engine.add(dl, start=start, force=start and not playlist)
            except ValueError:
                continue  # a playlist entry without a web address
            added += 1
        if added:
            self.feedback.play("added")
        if len(items) > 1:
            skipped = len(items) - added
            self.feedback.say(f"{added} items added" + (f", {skipped} skipped without a web address" if skipped else ""))

    def ensure_ytdlp(self) -> bool:
        if media.find_ytdlp(self.settings):
            return True
        if not forms.confirm(
            self._parent(),
            "Downpour uses the free yt-dlp program to grab video and audio from over a thousand sites. "
            "Download the official copy from GitHub now (about 18 MB)?",
        ):
            return False
        return self._install("Installing yt-dlp", "Downloading yt-dlp:", media.install_ytdlp)

    def ensure_ffmpeg(self, optional: bool = False) -> bool:
        if media.find_ffmpeg(self.settings):
            return True
        question = ("Joining separate video and audio streams needs the free FFmpeg program. "
                    "Download it from GitHub now (about 85 MB)?")
        if optional:
            question += " If you choose No, Downpour gets the best single file instead, which may be lower quality."
        if not forms.confirm(self._parent(), question):
            return False
        return self._install("Installing FFmpeg", "Downloading FFmpeg:", media.install_ffmpeg)

    def _install(self, title: str, message: str, installer) -> bool:
        finished, result = run_with_progress(self._parent(), title, message, installer)
        if not finished:
            log.info("%s: cancelled", title)
            return False
        if isinstance(result, BaseException):
            log.error("%s failed", title, exc_info=(type(result), result, result.__traceback__))
            forms.message(self._parent(), f"{title} failed: {result}", icon=wx.ICON_ERROR)
            return False
        self.feedback.say(f"{title.replace('Installing', '').strip()} is installed")
        return True

    # -- other dialogs ----------------------------------------------------------

    def properties_dialog(self, dl: Download) -> None:
        with PropertiesDialog(self._parent(), dl, self.engine.is_running(dl.id), self.settings.recent_folders) as dlg:
            if self._modal(dlg) != wx.ID_OK:
                return
            values, new_url = dlg.values, dlg.new_url
            new_name, new_folder = dlg.new_name, dlg.new_folder
        for key, value in values.items():
            setattr(dl, key, value)
        self.engine.update(dl)
        if (new_name and new_name != dl.filename) or (new_folder and os.path.normcase(new_folder) != os.path.normcase(dl.directory)):
            try:
                self.engine.relocate(dl.id, new_name, new_folder)
                self.feedback.say(f"Now {dl.display_name()}")
            except ValueError as e:
                forms.message(self._parent(), str(e), icon=wx.ICON_WARNING)
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
        now = asdict(self.settings)
        changes = settings_mod.differences(self._applied, now)
        if changes:
            log.info("settings changed: %s", "; ".join(changes))
        self._applied = now
        logs.set_detailed(self.settings.detailed_logging or self.debug or logs.forced_detailed())
        settings_mod.save(self.settings)
        self.engine.apply_settings()
        try:
            winutil.set_start_with_windows(self.settings.start_with_windows, winutil.autostart_command())
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

    # -- updates ---------------------------------------------------------------------

    def note_version(self) -> None:
        """Once after an update: say so, and clear out the downloaded setup."""
        previous = self.settings.last_run_version
        if previous == __version__:
            return
        if previous:
            log.info("updated from %s to %s", previous, __version__)
            wx.CallLater(1500, self.feedback.say, f"Downpour updated to version {__version__}")
        self.settings.last_run_version = __version__
        settings_mod.save(self.settings)
        updates.clean_downloads()

    def _maybe_check_updates(self) -> None:
        s = self.settings
        if (not s.check_for_updates or self._update_checking or not updates.supported()
                or time.monotonic() - self._started < 30
                or time.time() - s.last_update_check < updates.CHECK_INTERVAL):
            return
        self.check_updates(manual=False)

    def check_updates(self, manual: bool = True) -> None:
        """Ask GitHub for a newer release, on a thread. ``manual`` (the Help
        menu) always reports the outcome; an automatic check stays quiet
        unless there is something to offer."""
        if not updates.supported():
            if manual:
                forms.message(self._parent(), "Updates are for the installed copy of Downpour; this one runs from source.")
            return
        if self._update_checking:
            return
        self._update_checking = True
        if manual:
            self.feedback.say("Checking for updates")

        def work():
            try:
                release, error = updates.latest(), None
            except Exception as e:  # noqa: BLE001 - reported below
                release, error = None, e
                log.warning("update check failed: %s", logs.describe_error(e))
            wx.CallAfter(self._update_checked, release, error, manual)

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _update_checked(self, release, error, manual: bool) -> None:
        self._update_checking = False
        self.settings.last_update_check = time.time()
        settings_mod.save(self.settings)
        if error is not None:
            if manual:
                forms.message(self._parent(), f"Could not check for updates: {error}", icon=wx.ICON_WARNING)
            return
        if release is None or not updates.is_newer(release.version):
            if manual:
                forms.message(self._parent(), f"You have the newest version of Downpour, {__version__}.")
            return
        if not manual and release.version == self.settings.skipped_version:
            log.info("update %s is available but the user skipped it", release.version)
            return
        self.feedback.say(f"Downpour {release.version} is available")
        self._queue_dialog(lambda: self._offer_update(release))

    def _offer_update(self, release) -> None:
        notes = release.notes if len(release.notes) <= 1500 else release.notes[:1500] + "..."
        msg = (f"Downpour {release.version} is available. You have {__version__}.\n\n"
               f"{notes or 'The release has no notes.'}\n\n"
               "Updating closes Downpour for a moment. Downloads in progress carry on afterwards.")
        with wx.MessageDialog(self._parent(), msg, "Update available", wx.YES_NO | wx.CANCEL | wx.ICON_INFORMATION) as dlg:
            # Escape means Cancel, so Cancel must be the harmless "Later".
            dlg.SetYesNoCancelLabels("&Update now", "&Skip this version", "&Later")
            answer = self._modal(dlg)
        if answer == wx.ID_YES:
            self._install_update(release)
        elif answer == wx.ID_NO:
            log.info("the user skipped update %s", release.version)
            self.settings.skipped_version = release.version
            settings_mod.save(self.settings)
        else:
            log.info("the user put off update %s", release.version)

    def _install_update(self, release) -> None:
        finished, result = run_with_progress(
            self._parent(), "Updating Downpour", f"Downloading Downpour {release.version}:",
            lambda progress, stop: updates.download(release, progress, stop),
        )
        if not finished:
            log.info("update download cancelled")
            return
        if isinstance(result, BaseException):
            log.error("update download failed", exc_info=(type(result), result, result.__traceback__))
            forms.message(self._parent(), f"The update could not be downloaded: {result}", icon=wx.ICON_ERROR)
            return
        try:
            updates.install(result)
        except OSError as e:
            forms.message(self._parent(), f"The update could not be started: {e}", icon=wx.ICON_ERROR)
            return
        self.feedback.say("Installing the update. Downpour restarts in a moment.")

    def open_log_folder(self) -> None:
        try:
            winutil.open_path(str(logs_dir()))
        except OSError as e:
            forms.message(self._parent(), f"Could not open the log folder {logs_dir()}: {e}", icon=wx.ICON_ERROR)

    def log_environment(self) -> None:
        """Start-up facts that explain most reports: what differs from the
        defaults, whether speech and the browser helper work, and so on."""
        changed = settings_mod.differences(settings_mod.Settings(), self.settings)
        log.info("settings that differ from the defaults: %s", "; ".join(changed) or "none")
        speech = self.feedback.speech
        log.info(
            "speech: NVDA controller client %s, NVDA %s",
            "loaded" if speech.loaded else "missing", "running" if speech.available else "not running",
        )
        try:
            from downpour.browser import install

            report = install.status()
            log.info("browser helper: %s", "; ".join(f"{name} {state}" for name, state in report.items()))
            log.debug("browser helper program: %s", install.host_program())
        except Exception as e:  # noqa: BLE001 - only for the log
            log.warning("could not read the browser helper status: %r", e)
        log.info("start with Windows: %s", winutil.start_with_windows_command() or "not set")
        log.info(
            "yt-dlp at %s, FFmpeg at %s",
            media.find_ytdlp(self.settings) or "nowhere", media.find_ffmpeg(self.settings) or "nowhere",
        )

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
        log.info("every download has finished; when done: %s", action)
        if action == "nothing":
            return
        self.feedback.announce("All downloads finished", "", sound="queue-done", spoken="All downloads finished")

        def show():
            with CountdownDialog(self._parent(), action) as dlg:
                if self._modal(dlg) != wx.ID_OK:
                    log.info("when-done action %s cancelled by the user", action)
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
        log.info("exiting%s", " and then running the when-done action" if then else "")
        self._exiting = True
        winutil.keep_awake(False)
        settings_mod.save(self.settings)
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
            log.debug("clipboard: %s is not a file type to take over", redact_url(url))
            return
        self._last_url = url
        log.info("clipboard: offering %s", redact_url(url))
        self.ctl.offer({"url": url})


def _setup_logging() -> Path:
    path = logs.setup("downpour.log", detailed=True)  # settings decide the level once they are loaded

    def tell_user(value: BaseException) -> None:
        wx.MessageBox(f"Something went wrong: {value}\n\nDetails are in {path}", "Downpour error", wx.OK | wx.ICON_ERROR)

    logs.install_excepthooks(on_error=tell_user)
    return path


def run(urls: list[str] | None = None, tray: bool = False, debug: bool = False) -> int:
    _setup_logging()
    settings = settings_mod.load()
    debug = debug or logs.forced_detailed()
    logs.set_detailed(settings.detailed_logging or debug)
    ready = threading.Event()
    holder: dict[str, Controller] = {}

    def handler(msg: dict) -> dict:
        if not ready.wait(30):
            return {"ok": False, "error": "Downpour is still starting"}
        return holder["ctl"].handle_request(msg)

    try:
        server = ipc.Server(handler)
    except ipc.AlreadyRunning:
        if tray and not urls:
            log.info("started with Windows while already running; nothing to do")
            return 0  # started with Windows while already running: stay out of sight
        message = {"type": "add", "items": [{"url": u} for u in urls], "pick": len(urls) > 1} if urls else {"type": "show"}
        log.info("another copy is running; passing it a %s request", message["type"])
        try:
            ipc.request(message)
        except Exception:  # noqa: BLE001 - this copy is exiting either way
            log.warning("could not reach the running copy", exc_info=True)
        return 0

    logs.banner("window app", sys.argv[1:])
    logs.enable_crash_log()
    app = wx.App(False)
    app.SetAppName(APP_NAME)
    if settings.dark_mode and hasattr(app, "MSWEnableDarkMode"):
        try:
            app.MSWEnableDarkMode()
            log.info("dark mode on")
        except Exception:  # noqa: BLE001 - only looks
            log.warning("dark mode could not be turned on", exc_info=True)
    log.info("wxPython %s", wx.version())
    engine = Engine(Store(data_dir() / "downpour.db"), settings)
    ctl = Controller(app, settings, engine, debug=debug)
    holder["ctl"] = ctl
    ctl.log_environment()
    ctl.note_version()
    server.start()
    ready.set()
    engine.begin()
    watchdog = logs.Watchdog(wx.CallAfter)
    watchdog.start()
    if not tray:
        ctl.show_window()
    if urls:
        wx.CallAfter(ctl.add_urls, urls)
    app.MainLoop()
    watchdog.stop()
    log.info("window closed; stopping")
    server.close()
    engine.shutdown()
    after = getattr(ctl, "_after_exit", None)
    if after is not None:
        after()
    log.info("stopped")
    logging.shutdown()
    return 0
