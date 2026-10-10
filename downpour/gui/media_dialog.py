"""Grab video or audio from a page with yt-dlp.

The dialog opens at once and asks yt-dlp about the page in the background.
When the answer arrives, focus moves to the quality list (only if the user
is still on the summary), so the screen reader announces that it is ready.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import wx

from downpour.core import media
from downpour.core.settings import Settings
from downpour.core.units import format_duration, format_size
from downpour.gui import forms
from downpour.gui.dialogs import folder_for_category


class MediaDialog(wx.Dialog):
    def __init__(self, parent: wx.Window | None, settings: Settings, url: str,
                 cookies: list[dict] | None = None, user_agent: str = "", referrer: str = ""):
        super().__init__(parent, title="Grab video or audio", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.settings = settings
        self.url = url
        self.cookies = cookies or []
        self.user_agent = user_agent
        self.referrer = referrer
        self.info: media.MediaInfo | None = None
        self.choices: list[media.Choice] = list(media.PRESETS)
        self.start_now = True
        self._alive = True
        self._name_edited = False
        self._folder_edited = False
        self._fetching = True
        self._stop = threading.Event()

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.summary = forms.readonly(self, sizer, "Page:", "Getting information about this page...")
        sizer.Add(wx.StaticText(self, label="&Quality:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.quality = wx.ListBox(self, choices=[c.label for c in self.choices], style=wx.LB_SINGLE)
        self.quality.SetMinSize((-1, 180))
        sizer.Add(self.quality, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.name = forms.text(self, sizer, "File &name:", "")
        self.folder = forms.folder_row(self, sizer, "Save &to:", folder_for_category(settings, "Video"), settings.recent_folders)
        self.whole_playlist = forms.check(self, sizer, "Download every item in the &playlist", True)
        self.whole_playlist.Hide()
        self.audio_language = forms.text(
            self, sizer, "Audio track lan&guage (for example en, fr, or original; empty for the site's default):",
            settings.audio_language,
        )
        self.subtitles = forms.choice(self, sizer, "Su&btitles:", [label for _, label in media.SUBTITLE_MODES],
                                      next((i for i, (k, _) in enumerate(media.SUBTITLE_MODES) if k == settings.subtitles), 0))
        self.subtitle_language = forms.text(self, sizer, "Subtitle language (for example en):", settings.subtitle_language)
        self.sponsorblock = forms.check(self, sizer, "Cut sponsor segments (Sponsor&Block)", settings.sponsorblock)
        self.metadata = forms.check(self, sizer, "Tags, chapters and cover art in the fi&le", settings.media_metadata)
        self.clip_start = forms.text(self, sizer, "Only from (for example 1:30; empty for the start):", "")
        self.clip_end = forms.text(self, sizer, "to (for example 12:00; empty for the end):", "")
        btns = forms.buttons(
            self, sizer,
            [(wx.ID_OK, "&Start download"), (wx.ID_APPLY, "Download &later"), (wx.ID_CANCEL, "Cancel")],
            default=wx.ID_OK,
        )
        self.SetSizer(sizer)
        self.SetSize((720, 760))
        self.CentreOnParent()
        default = next((i for i, c in enumerate(self.choices) if c.key == settings.media_format), 0)
        self.quality.SetSelection(default)
        self.summary.SetFocus()

        self.quality.Bind(wx.EVT_LISTBOX, self._on_quality)
        self.name.Bind(wx.EVT_TEXT, lambda e: setattr(self, "_name_edited", True) or e.Skip())
        self.folder.Bind(wx.EVT_TEXT, lambda e: setattr(self, "_folder_edited", True) or e.Skip())
        btns[wx.ID_OK].Bind(wx.EVT_BUTTON, lambda e: self._finish(True))
        btns[wx.ID_APPLY].Bind(wx.EVT_BUTTON, lambda e: self._finish(False))
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._folder_edited = False
        threading.Thread(target=self._fetch, daemon=True).start()

    def _on_destroy(self, evt):
        if evt.GetEventObject() is self:
            self._alive = False
            self._stop.set()
        evt.Skip()

    def _fetch(self):
        try:
            info = media.fetch_info(self.url, self.settings, self.cookies, self.user_agent, self.referrer, stop=self._stop)
        except InterruptedError:
            return
        except Exception as e:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._fetched, None, str(e))
            return
        wx.CallAfter(self._fetched, info, "")

    def _fetched(self, info: media.MediaInfo | None, error: str):
        if not self._alive:
            return
        self._fetching = False
        focus_was_summary = wx.Window.FindFocus() is self.summary
        if info is None:
            self.summary.SetValue(f"yt-dlp could not read this page: {error}")
            return
        self.info = info
        parts = [info.title]
        if info.is_playlist:
            parts.append(f"playlist of {len(info.entries)} items")
        if info.duration:
            parts.append(format_duration(info.duration))
        if info.size:
            parts.append(f"about {format_size(info.size)}")
        if info.extractor:
            parts.append(f"from {info.extractor}")
        if info.uploader:
            parts.append(f"by {info.uploader}")
        self.summary.SetValue(", ".join(parts))
        if info.is_playlist:
            self.whole_playlist.SetLabel(f"Download all {len(info.entries)} items in the &playlist")
            self.whole_playlist.Show()
            self.Layout()
        if info.formats:
            selected = self.quality.GetSelection()
            self.choices = list(media.PRESETS) + info.formats
            self.quality.Set([c.label for c in self.choices])
            self.quality.SetSelection(max(0, selected))
        self._sync()
        if focus_was_summary:
            self.quality.SetFocus()

    def _choice(self) -> media.Choice:
        i = self.quality.GetSelection()
        return self.choices[i if 0 <= i < len(self.choices) else 0]

    def _on_quality(self, evt):
        self._sync()
        evt.Skip()

    def _sync(self):
        choice = self._choice()
        if self.info is not None and not self._name_edited and not self.info.is_playlist:
            self.name.ChangeValue(media.suggested_name(self.info, choice))
        if not self._folder_edited:
            self.folder.ChangeValue(folder_for_category(self.settings, "Music" if choice.audio_only else "Video"))

    def _finish(self, start: bool):
        if self._fetching:
            # Without the page's details the file would be named after the
            # address ("watch"), and a playlist would come as one item.
            forms.message(self, "Downpour is still reading the page. Try again in a moment.")
            return
        folder = self.folder.GetValue().strip()
        if not folder:
            forms.message(self, "Choose a folder to save into.", icon=wx.ICON_WARNING)
            self.folder.SetFocus()
            return
        for ctrl in (self.clip_start, self.clip_end):
            if ctrl.GetValue().strip() and not TIME_RE.match(ctrl.GetValue().strip()):
                forms.message(self, "Write the time like 90, 1:30 or 0:01:30.", icon=wx.ICON_WARNING)
                ctrl.SetFocus()
                return
        self.start_now = start
        self.chosen = self._choice()
        self.filename = self.name.GetValue().strip()
        self.directory = folder
        self.media_options = {
            "audio_language": self.audio_language.GetValue().strip(),
            "subtitles": media.SUBTITLE_MODES[self.subtitles.GetSelection()][0],
            "subtitle_language": self.subtitle_language.GetValue().strip() or "en",
            "sponsorblock": self.sponsorblock.GetValue(),
            "metadata": self.metadata.GetValue(),
            "clip_start": self.clip_start.GetValue().strip(),
            "clip_end": self.clip_end.GetValue().strip(),
        }
        self.playlist = bool(self.info and self.info.is_playlist and self.whole_playlist.GetValue())
        self.EndModal(wx.ID_OK)


TIME_RE = re.compile(r"^\d+(:[0-5]?\d){0,2}(\.\d+)?$")


def media_format_value(choice: media.Choice) -> str:
    """How a choice is stored on a Download: a preset key, or id:...|selector."""
    return f"{choice.key}|{choice.selector}" if choice.key.startswith("id:") else choice.key


def run_with_progress(parent: wx.Window | None, title: str, message: str, work) -> tuple[bool, object]:
    """Run ``work(progress, stop)`` on a thread behind a progress dialog.

    Returns (finished, result or exception). Cancel sets ``stop``.
    """
    stop = threading.Event()
    state = {"done": 0, "total": None, "result": None, "error": None, "finished": False}

    def progress(done: int, total: int | None):
        state["done"], state["total"] = done, total

    def runner():
        try:
            state["result"] = work(progress, stop)
        except BaseException as e:  # noqa: BLE001 - reported to the caller
            state["error"] = e
        state["finished"] = True

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    dlg = wx.ProgressDialog(title, message, maximum=100, parent=parent,
                            style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE)
    try:
        while not state["finished"]:
            wx.MilliSleep(150)
            total = state["total"]
            if total:
                pct = min(99, int(state["done"] * 100 / total))
                keep, _ = dlg.Update(pct, f"{message} {format_size(state['done'])} of {format_size(total)}")
            else:
                keep, _ = dlg.Pulse(f"{message} {format_size(state['done'])}")
            if not keep:
                stop.set()
                thread.join(5)
                return False, None
    finally:
        dlg.Destroy()
    if state["error"] is not None:
        return True, state["error"]
    return True, state["result"]
