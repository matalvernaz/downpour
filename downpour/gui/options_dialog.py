"""The Options dialog. Edits a copy of the settings; OK copies them back."""

from __future__ import annotations

import copy
import os
import re
import sys
from dataclasses import fields

import wx

from downpour.core import media, paths
from downpour.core.settings import Category, Settings
from downpour.core.units import format_size, parse_size
from downpour.gui import forms
from downpour.gui.a11y import set_accessible_name

PROXY_MODES = [("system", "Use the Windows proxy settings"), ("none", "Connect directly, without a proxy"), ("manual", "Use this proxy")]
DUPLICATES = [("rename", "Add a number to the new file's name"), ("overwrite", "Replace the existing file")]
WHEN_DONE = [("nothing", "Do nothing"), ("exit", "Close Downpour"), ("sleep", "Sleep"), ("hibernate", "Hibernate"), ("shutdown", "Shut down")]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _index(pairs, key) -> int:
    return next((i for i, (k, _) in enumerate(pairs) if k == key), 0)


def _size_field(n: int) -> str:
    return format_size(n).replace(" ", "").replace("bytes", "") if n else "0"


class OptionsDialog(wx.Dialog):
    def __init__(self, parent: wx.Window | None, settings: Settings, page: int = 0):
        super().__init__(parent, title="Downpour options", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.live = settings
        self.s = copy.deepcopy(settings)
        outer = wx.BoxSizer(wx.VERTICAL)
        self.book = wx.Notebook(self)
        self._general()
        self._connection()
        self._saving()
        self._browser()
        self._scheduler()
        self._media()
        outer.Add(self.book, 1, wx.EXPAND | wx.ALL, 6)
        forms.buttons(self, outer, [(wx.ID_OK, "OK"), (wx.ID_CANCEL, "Cancel")], default=wx.ID_OK)
        self.SetSizer(outer)
        self.SetSize((760, 640))
        self.CentreOnParent()
        self.book.SetSelection(page)
        self.book.SetFocus()
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _page(self, title: str) -> tuple[wx.ScrolledWindow, wx.BoxSizer]:
        page = wx.ScrolledWindow(self.book)
        page.SetScrollRate(0, 20)
        sizer = wx.BoxSizer(wx.VERTICAL)
        page.SetSizer(sizer)
        self.book.AddPage(page, title)
        return page, sizer

    # -- pages ----------------------------------------------------------------

    def _general(self):
        p, sz = self._page("General")
        s = self.s
        self.start_with_windows = forms.check(p, sz, "Start Downpour when &Windows starts, in the notification area", s.start_with_windows)
        self.close_to_tray = forms.check(p, sz, "&Closing the window keeps Downpour running in the notification area", s.close_to_tray)
        self.clipboard_monitor = forms.check(p, sz, "Watch the clip&board for download links", s.clipboard_monitor)
        self.show_info_dialog = forms.check(p, sz, "As&k before starting downloads from the browser or clipboard", s.show_info_dialog)
        self.show_complete_dialog = forms.check(p, sz, "Show a dialog when a download &finishes", s.show_complete_dialog)
        self.speak_events = forms.check(p, sz, "&Speak events through NVDA", s.speak_events)
        self.sound_complete = forms.check(p, sz, "&Play sounds", s.sound_complete)
        self.notify_complete = forms.check(p, sz, "Show Windows &notifications when nothing was spoken", s.notify_complete)
        self.scan_after_download = forms.check(p, sz, "Scan finished downloads with Microsoft &Defender", s.scan_after_download)
        self.mark_of_the_web = forms.check(p, sz, "&Mark downloaded files as coming from the internet (recommended)", s.mark_of_the_web)
        self.detailed_logging = forms.check(p, sz, "Write detailed &logs for troubleshooting", s.detailed_logging)
        logs_button = wx.Button(p, label="Open the log &folder")
        logs_button.Bind(wx.EVT_BUTTON, self._open_logs)
        sz.Add(logs_button, 0, wx.ALL, 6)

    def _open_logs(self, evt):
        folder = paths.logs_dir()
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606
        else:
            forms.message(self, f"The logs are in {folder}.")

    def _connection(self):
        p, sz = self._page("Connection")
        s = self.s
        self.max_connections = forms.spin(p, sz, "&Connections per download:", s.max_connections, 1, 32)
        self.max_active = forms.spin(p, sz, "&Downloads running at the same time:", s.max_active, 1, 20)
        self.speed_limit_enabled = forms.check(p, sz, "&Limit the total download speed", s.speed_limit_enabled)
        self.speed_limit = forms.text(p, sz, "&Speed limit (for example 500K or 2M):", _size_field(s.speed_limit) if s.speed_limit else "1M")
        self.timeout = forms.spin(p, sz, "Connection &timeout in seconds:", int(s.timeout), 5, 300)
        self.max_retries = forms.spin(p, sz, "&Retries before a download fails:", s.max_retries, 0, 100)
        self.proxy_mode = forms.choice(p, sz, "Pro&xy:", [label for _, label in PROXY_MODES], _index(PROXY_MODES, s.proxy_mode))
        self.proxy_url = forms.text(p, sz, "Proxy &address (for example http://127.0.0.1:8080):", s.proxy_url)
        self.user_agent = forms.text(p, sz, "&User agent sent to servers:", s.user_agent)

    def _saving(self):
        p, sz = self._page("Saving")
        s = self.s
        self.download_dir = forms.folder_row(p, sz, "Download &folder:", s.download_dir)
        self.use_category_folders = forms.check(p, sz, "Sort downloads into &category folders", s.use_category_folders)
        self.duplicate_action = forms.choice(
            p, sz, "When a file with the same name &exists:", [label for _, label in DUPLICATES], _index(DUPLICATES, s.duplicate_action)
        )
        sz.Add(wx.StaticText(p, label="Cate&gories:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.cat_list = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        set_accessible_name(self.cat_list, "Categories")
        self.cat_list.InsertColumn(0, "Category", width=140)
        self.cat_list.InsertColumn(1, "File types", width=360)
        self.cat_list.InsertColumn(2, "Folder", width=200)
        self.cat_list.SetMinSize((-1, 150))
        sz.Add(self.cat_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.cat_types = forms.text(p, sz, "File &types of the selected category (separated by spaces):", "")
        self.cat_folder = forms.folder_row(p, sz, "F&older of the selected category (empty for a subfolder of the download folder):", "")
        self._cat_index = -1
        self._fill_categories()
        self.cat_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_cat_selected)
        self.cat_types.Bind(wx.EVT_TEXT, self._on_cat_edit)
        self.cat_folder.Bind(wx.EVT_TEXT, self._on_cat_edit)
        if self.s.categories:
            self.cat_list.Select(0)
            self.cat_list.Focus(0)

    def _fill_categories(self):
        self.cat_list.DeleteAllItems()
        for i, cat in enumerate(self.s.categories):
            self.cat_list.InsertItem(i, cat.name)
            self.cat_list.SetItem(i, 1, " ".join(cat.extensions))
            self.cat_list.SetItem(i, 2, cat.folder or "(automatic)")

    def _on_cat_selected(self, evt):
        self._cat_index = evt.GetIndex()
        cat = self.s.categories[self._cat_index]
        self.cat_types.ChangeValue(" ".join(cat.extensions))
        self.cat_folder.ChangeValue(cat.folder)

    def _on_cat_edit(self, evt):
        i = self._cat_index
        if 0 <= i < len(self.s.categories):
            cat = self.s.categories[i]
            cat.extensions = [e.lower().lstrip(".") for e in self.cat_types.GetValue().split()]
            cat.folder = self.cat_folder.GetValue().strip()
            self.cat_list.SetItem(i, 1, " ".join(cat.extensions))
            self.cat_list.SetItem(i, 2, cat.folder or "(automatic)")
        evt.Skip()

    def _browser(self):
        p, sz = self._page("Browser integration")
        s = self.s
        self.browser_capture = forms.check(p, sz, "&Take over downloads from the browser", s.browser_capture)
        self.capture_extensions = forms.text(
            p, sz, "&File types to take over (separated by spaces):", " ".join(s.capture_extensions),
            style=wx.TE_MULTILINE, proportion=0,
        )
        self.capture_extensions.SetMinSize((-1, 70))
        self.capture_min_size = forms.text(
            p, sz, "Leave files smaller than this to the &browser (for example 100K; 0 for none):", _size_field(s.capture_min_size)
        )
        self.browser_status = forms.readonly(p, sz, "Browser helper &status:", self._browser_status(), lines=6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (
            ("&Install browser helper", self._install_helper),
            ("&Remove browser helper", self._remove_helper),
            ("Open the &extension folder", self._open_extension_folder),
        ):
            b = wx.Button(p, label=label)
            b.Bind(wx.EVT_BUTTON, handler)
            row.Add(b, 0, wx.RIGHT, 6)
        sz.Add(row, 0, wx.ALL, 6)
        sz.Add(
            wx.StaticText(p, label="The browser helper lets the Downpour extension talk to Downpour. "
                          "Install it once, then add the extension to your browser: see docs\\browser-extension.md."),
            0, wx.ALL, 6,
        )

    def _browser_status(self) -> str:
        try:
            from downpour.browser import install
        except ImportError:
            return "Browser integration is not available in this build."
        try:
            report = install.status()
        except Exception as e:  # noqa: BLE001
            return f"Could not read the browser helper status: {e}"
        if isinstance(report, dict):
            return "\n".join(f"{name}: {state}" for name, state in report.items())
        return str(report)

    def _install_helper(self, evt):
        try:
            from downpour.browser import install

            install.register()
        except Exception as e:  # noqa: BLE001
            forms.message(self, f"Could not install the browser helper: {e}", icon=wx.ICON_ERROR)
        self.browser_status.SetValue(self._browser_status())
        self.browser_status.SetFocus()

    def _remove_helper(self, evt):
        try:
            from downpour.browser import install

            install.unregister()
        except Exception as e:  # noqa: BLE001
            forms.message(self, f"Could not remove the browser helper: {e}", icon=wx.ICON_ERROR)
        self.browser_status.SetValue(self._browser_status())
        self.browser_status.SetFocus()

    def _open_extension_folder(self, evt):
        folder = paths.app_dir() / "extension"
        if folder.is_dir() and sys.platform == "win32":
            os.startfile(folder)  # noqa: S606
        else:
            forms.message(self, f"The extension folder was not found at {folder}.", icon=wx.ICON_WARNING)

    def _scheduler(self):
        p, sz = self._page("Scheduler")
        s = self.s
        self.schedule_enabled = forms.check(p, sz, "&Start paused and failed downloads automatically on a schedule", s.schedule_enabled)
        self.schedule_start = forms.text(p, sz, "Start &time, 24-hour clock (for example 02:00):", s.schedule_start)
        self.schedule_stop = forms.text(p, sz, "Sto&p time (optional, pauses everything):", s.schedule_stop)
        box = wx.StaticBox(p, label="On these days")
        box_sizer = wx.StaticBoxSizer(box, wx.VERTICAL)
        self.days = []
        for i, day in enumerate(DAYS):
            cb = wx.CheckBox(box, label=day)
            cb.SetValue(i in s.schedule_days)
            box_sizer.Add(cb, 0, wx.ALL, 3)
            self.days.append(cb)
        sz.Add(box_sizer, 0, wx.EXPAND | wx.ALL, 6)
        self.when_done = forms.choice(
            p, sz, "&When all downloads finish:", [label for _, label in WHEN_DONE], _index(WHEN_DONE, s.when_done)
        )

    def _media(self):
        p, sz = self._page("Media")
        s = self.s
        labels = [c.label for c in media.PRESETS]
        default = next((i for i, c in enumerate(media.PRESETS) if c.key == s.media_format), 0)
        self.media_format = forms.choice(p, sz, "Default &quality for video and audio:", labels, default)
        self.tool_status = forms.readonly(p, sz, "&Helper programs:", self._tool_text(), lines=3)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (
            ("Install or &update yt-dlp", self._ytdlp_button),
            ("Install &FFmpeg", self._ffmpeg_button),
        ):
            b = wx.Button(p, label=label)
            b.Bind(wx.EVT_BUTTON, handler)
            row.Add(b, 0, wx.RIGHT, 6)
        sz.Add(row, 0, wx.ALL, 6)
        self.ytdlp_path = forms.text(p, sz, "yt-dlp &program (leave empty to find it automatically):", s.ytdlp_path)
        self.ffmpeg_path = forms.text(p, sz, "FFmpeg program or f&older (leave empty to find it automatically):", s.ffmpeg_path)

    def _tool_text(self) -> str:
        ytdlp = media.find_ytdlp(self.s)
        ffmpeg = media.find_ffmpeg(self.s)
        return "\n".join([
            f"yt-dlp: {ytdlp or 'not installed'}",
            f"FFmpeg: {ffmpeg or 'not installed (needed for the best video quality and MP3)'}",
        ])

    def _ytdlp_button(self, evt):
        from downpour.gui.media_dialog import run_with_progress

        path = media.find_ytdlp(self.s)
        if path:
            finished, result = run_with_progress(self, "Updating yt-dlp", "Checking for a newer yt-dlp...",
                                                 lambda progress, stop: media.update_ytdlp(path))
            if finished:
                forms.message(self, str(result) or "yt-dlp has been checked.", title="yt-dlp update")
        else:
            finished, result = run_with_progress(self, "Installing yt-dlp", "Downloading yt-dlp:", media.install_ytdlp)
            if finished and isinstance(result, BaseException):
                forms.message(self, f"Installing yt-dlp failed: {result}", icon=wx.ICON_ERROR)
        self.tool_status.SetValue(self._tool_text())
        self.tool_status.SetFocus()

    def _ffmpeg_button(self, evt):
        from downpour.gui.media_dialog import run_with_progress

        if media.find_ffmpeg(self.s) and not forms.confirm(self, "FFmpeg is already available. Download Downpour's own copy anyway?"):
            return
        finished, result = run_with_progress(self, "Installing FFmpeg", "Downloading FFmpeg:", media.install_ffmpeg)
        if finished and isinstance(result, BaseException):
            forms.message(self, f"Installing FFmpeg failed: {result}", icon=wx.ICON_ERROR)
        self.tool_status.SetValue(self._tool_text())
        self.tool_status.SetFocus()

    # -- saving ---------------------------------------------------------------

    def _fail(self, page: int, ctrl: wx.Window, text: str) -> None:
        self.book.SetSelection(page)
        forms.message(self, text, icon=wx.ICON_WARNING)
        ctrl.SetFocus()

    def _on_ok(self, evt):
        s = self.s
        limit = parse_size(self.speed_limit.GetValue())
        if self.speed_limit_enabled.GetValue() and not limit:
            return self._fail(1, self.speed_limit, "Write the speed limit like 500K or 2M.")
        min_size = parse_size(self.capture_min_size.GetValue() or "0")
        if min_size is None:
            return self._fail(3, self.capture_min_size, "Write the size like 100K, or 0.")
        start, stop = self.schedule_start.GetValue().strip(), self.schedule_stop.GetValue().strip()
        if self.schedule_enabled.GetValue() and not TIME_RE.match(start):
            return self._fail(4, self.schedule_start, "Write the start time like 02:00.")
        if stop and not TIME_RE.match(stop):
            return self._fail(4, self.schedule_stop, "Write the stop time like 07:30, or leave it empty.")
        folder = self.download_dir.GetValue().strip()
        if not folder:
            return self._fail(2, self.download_dir, "Choose a download folder.")
        if self.proxy_mode.GetSelection() == 2 and not self.proxy_url.GetValue().strip():
            return self._fail(1, self.proxy_url, "Enter the proxy address, or choose another proxy option.")

        for name in (
            "start_with_windows", "close_to_tray", "clipboard_monitor", "show_info_dialog",
            "show_complete_dialog", "speak_events", "sound_complete", "notify_complete",
            "scan_after_download", "mark_of_the_web", "speed_limit_enabled", "use_category_folders",
            "browser_capture", "schedule_enabled", "detailed_logging",
        ):
            setattr(s, name, getattr(self, name).GetValue())
        s.max_connections = self.max_connections.GetValue()
        s.max_active = self.max_active.GetValue()
        s.speed_limit = limit or s.speed_limit
        s.timeout = float(self.timeout.GetValue())
        s.max_retries = self.max_retries.GetValue()
        s.proxy_mode = PROXY_MODES[self.proxy_mode.GetSelection()][0]
        s.proxy_url = self.proxy_url.GetValue().strip()
        s.user_agent = self.user_agent.GetValue().strip() or Settings().user_agent
        s.download_dir = folder
        s.duplicate_action = DUPLICATES[self.duplicate_action.GetSelection()][0]
        s.capture_extensions = [e.lower().lstrip(".") for e in self.capture_extensions.GetValue().split()]
        s.capture_min_size = min_size
        s.schedule_start = start or s.schedule_start
        s.schedule_stop = stop
        s.schedule_days = [i for i, cb in enumerate(self.days) if cb.GetValue()]
        s.when_done = WHEN_DONE[self.when_done.GetSelection()][0]
        s.media_format = media.PRESETS[self.media_format.GetSelection()].key
        s.ytdlp_path = self.ytdlp_path.GetValue().strip()
        s.ffmpeg_path = self.ffmpeg_path.GetValue().strip()
        s.categories = [Category(c.name, c.extensions, c.folder) for c in s.categories]
        for f in fields(Settings):
            setattr(self.live, f.name, getattr(s, f.name))
        evt.Skip()
