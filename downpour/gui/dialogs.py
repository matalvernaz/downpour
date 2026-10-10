"""Dialogs for adding and inspecting downloads."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import wx

from downpour.core import checksum as checksum_mod
from downpour.core import filenames, net
from downpour.core.models import Download, Status
from downpour.core.settings import Settings
from downpour.core.units import format_duration, format_size, format_speed, parse_size
from downpour.gui import forms
from downpour.gui.a11y import set_accessible_name
from downpour.gui.download_list import size_text, status_text

URL_RE = re.compile(r"""https?://[^\s<>"'`]+""", re.I)
GENERAL = "General"


def _trim(url: str) -> str:
    """Drop punctuation that ends the sentence around a link, but keep a
    closing bracket the link opened itself, as in wiki/Python_(language)."""
    pairs = {")": "(", "]": "[", "}": "{"}
    while url:
        last = url[-1]
        if last in ".,;:!?'\"":
            url = url[:-1]
        elif last in pairs and url.count(pairs[last]) < url.count(last):
            url = url[:-1]
        else:
            break
    return url


def urls_in(text: str) -> list[str]:
    """Every http(s) link in ``text``, in order, without duplicates."""
    seen, out = set(), []
    for m in URL_RE.finditer(text or ""):
        url = _trim(m.group(0))
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def clipboard_text() -> str:
    if not wx.TheClipboard.Open():
        return ""
    try:
        data = wx.TextDataObject()
        if wx.TheClipboard.GetData(data):
            return data.GetText()
        return ""
    finally:
        wx.TheClipboard.Close()


def item_to_download(item: dict) -> Download:
    """Build a Download from a browser-extension item (see docs/browser-protocol.md)."""
    size = item.get("size")
    return Download(
        url=str(item.get("finalUrl") or item.get("url") or ""),
        filename=filenames.sanitize(item["filename"]) if item.get("filename") else "",
        filename_fixed=bool(item.get("filename")),
        referrer=str(item.get("referrer") or ""),
        page_url=str(item.get("pageUrl") or ""),
        description=str(item.get("pageTitle") or item.get("text") or ""),
        size=size if isinstance(size, int) and size >= 0 else None,
        mime=str(item.get("mime") or ""),
        cookies=[c for c in item.get("cookies") or [] if isinstance(c, dict)],
        user_agent=str(item.get("userAgent") or ""),
    )


def category_names(settings: Settings) -> list[str]:
    return [GENERAL] + [c.name for c in settings.categories]


def folder_for_category(settings: Settings, name: str) -> str:
    base = Path(settings.download_dir)
    if name == GENERAL or not settings.use_category_folders:
        return str(base)
    for cat in settings.categories:
        if cat.name == name:
            return cat.folder or str(base / cat.name)
    return str(base)


class AddUrlDialog(wx.Dialog):
    """Ask for an address. Pre-filled with a link from the clipboard."""

    def __init__(self, parent: wx.Window | None, initial: str = "", title: str = "Add download", label: str = "&Address:"):
        super().__init__(parent, title=title)
        sizer = wx.BoxSizer(wx.VERTICAL)
        if not initial:
            found = urls_in(clipboard_text())
            initial = found[0] if found else ""
        self.address = forms.text(self, sizer, label, initial)
        self.address.SetMinSize((520, -1))
        forms.buttons(self, sizer, [(wx.ID_OK, "OK"), (wx.ID_CANCEL, "Cancel")], default=wx.ID_OK)
        self.SetSizerAndFit(sizer)
        self.address.SetFocus()
        self.address.SelectAll()
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, evt):
        if not urls_in(self.address.GetValue()):
            forms.message(self, "Enter an address that starts with http:// or https://", icon=wx.ICON_WARNING)
            self.address.SetFocus()
            return
        evt.Skip()

    @property
    def urls(self) -> list[str]:
        return urls_in(self.address.GetValue())


class DownloadInfoDialog(wx.Dialog):
    """Confirm a new download: name, folder, then start now or later.

    The first control is a one-line summary, so the screen reader reads what
    is being downloaded as soon as the dialog opens. If the size is not known
    yet, the server is asked in the background and the summary updated.
    """

    START, LATER = 1, 2

    def __init__(self, parent: wx.Window | None, settings: Settings, dl: Download):
        super().__init__(parent, title="New download", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.settings = settings
        self.dl = dl
        self.choice = 0
        self._folder_edited = False
        self._name_edited = False
        self._alive = True
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.summary = forms.readonly(self, sizer, "Download:", self._summary_text())
        self.address = forms.readonly(self, sizer, "Address:", dl.url)
        self.name = forms.text(self, sizer, "File &name:", dl.filename or filenames.sanitize(filenames.from_url(dl.url)))
        cats = category_names(settings)
        cat = settings.category_for(self.name.GetValue(), dl.url)
        self.category = forms.choice(self, sizer, "&Category:", cats, cats.index(cat.name) if cat else 0)
        self.folder = forms.folder_row(self, sizer, "Save &to:", dl.directory or str(settings.folder_for(self.name.GetValue(), dl.url)),
                                       settings.recent_folders)
        self.checksum = forms.text(self, sizer, "E&xpected checksum (optional):", dl.checksum)
        btns = forms.buttons(
            self, sizer,
            [(wx.ID_OK, "&Start download"), (wx.ID_APPLY, "Download &later"), (wx.ID_CANCEL, "Cancel")],
            default=wx.ID_OK,
        )
        self.SetSizerAndFit(sizer)
        self.SetSize((max(self.GetSize().width, 600), -1))
        self.CentreOnParent()
        self.summary.SetFocus()

        self.name.Bind(wx.EVT_TEXT, self._on_name)
        self.folder.Bind(wx.EVT_TEXT, lambda e: setattr(self, "_folder_edited", True) or e.Skip())
        self.category.Bind(wx.EVT_CHOICE, self._on_category)
        btns[wx.ID_OK].Bind(wx.EVT_BUTTON, lambda e: self._finish(self.START))
        btns[wx.ID_APPLY].Bind(wx.EVT_BUTTON, lambda e: self._finish(self.LATER))
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._folder_edited = False
        if dl.size is None or not dl.filename_fixed:
            threading.Thread(target=self._probe, daemon=True).start()

    def _summary_text(self, note: str = "") -> str:
        dl = self.dl
        host = re.sub(r"^www\.", "", net.host_of(dl.url))
        size = format_size(dl.size) if dl.size is not None else "size not known yet"
        name = self.name.GetValue() if hasattr(self, "name") else dl.filename or filenames.from_url(dl.url)
        text = f"{name}, {size}, from {host}"
        return f"{text}. {note}" if note else text

    def _on_destroy(self, evt):
        if evt.GetEventObject() is self:
            self._alive = False
        evt.Skip()

    def _probe(self):
        dl = self.dl
        s = self.settings
        try:
            result = net.probe(
                dl.url, cookies=dl.cookies, referrer=dl.referrer,
                user_agent=dl.user_agent or s.user_agent, headers=dl.headers,
                username=dl.username, password=dl.password,
                proxy_mode=s.proxy_mode, proxy_url=s.proxy_url, timeout=15,
            )
        except Exception as e:  # noqa: BLE001 - shown to the user, never fatal
            wx.CallAfter(self._probed, None, net.describe(e))
            return
        wx.CallAfter(self._probed, result, "")

    def _probed(self, result, error: str):
        if not self._alive:
            return
        if result is None:
            self.summary.SetValue(self._summary_text(f"Could not check the file: {error}"))
            return
        if self.dl.size is None:
            self.dl.size = result.size
        if not self.dl.filename_fixed and not self._name_edited:
            self.name.ChangeValue(result.filename)
            self._sync_category()
        note = "" if result.resumable else "This server cannot resume, so a paused download starts over."
        self.summary.SetValue(self._summary_text(note))

    def _on_name(self, evt):
        self._name_edited = True
        self._sync_category()
        evt.Skip()

    def _sync_category(self):
        cat = self.settings.category_for(self.name.GetValue(), self.dl.url)
        names = category_names(self.settings)
        index = names.index(cat.name) if cat else 0
        if index != self.category.GetSelection():
            self.category.SetSelection(index)
            if not self._folder_edited:
                self.folder.ChangeValue(folder_for_category(self.settings, names[index]))

    def _on_category(self, evt):
        name = self.category.GetStringSelection()
        self.folder.ChangeValue(folder_for_category(self.settings, name))
        self._folder_edited = False

    def _finish(self, choice: int):
        name = filenames.sanitize(self.name.GetValue().strip())
        folder = self.folder.GetValue().strip()
        if not folder:
            forms.message(self, "Choose a folder to save into.", icon=wx.ICON_WARNING)
            self.folder.SetFocus()
            return
        expected = self.checksum.GetValue().strip()
        if expected and checksum_mod.parse(expected) is None:
            forms.message(self, "That does not look like an MD5, SHA-1, SHA-256 or SHA-512 checksum.", icon=wx.ICON_WARNING)
            self.checksum.SetFocus()
            return
        dl = self.dl
        dl.filename = name
        dl.filename_fixed = self._name_edited or dl.filename_fixed
        dl.directory = folder
        dl.checksum = expected
        self.choice = choice
        self.EndModal(wx.ID_OK)


class LinkPickerDialog(wx.Dialog):
    """Choose which of many links to download (a page's links, a batch, a paste).

    Uses a ListCtrl with checkboxes, which screen readers expose row by row
    with a checked state; wx.CheckListBox would show them an empty list.
    """

    def __init__(self, parent: wx.Window | None, settings: Settings, items: list[Download], title: str = "Choose downloads"):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.settings = settings
        self.items = items
        self.start_now = True
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=f"&Links ({len(items)}). Space checks or unchecks:"), 0, wx.ALL, 6)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.EnableCheckBoxes(True)
        set_accessible_name(self.list, "Links")
        self.list.InsertColumn(0, "Name", width=260)
        self.list.InsertColumn(1, "Address", width=380)
        self.list.InsertColumn(2, "Link text", width=200)
        for i, dl in enumerate(items):
            self.list.InsertItem(i, dl.filename or filenames.from_url(dl.url))
            self.list.SetItem(i, 1, dl.url)
            self.list.SetItem(i, 2, dl.description)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (
            ("Check &all", lambda e: self._check_all(True)),
            ("&Uncheck all", lambda e: self._check_all(False)),
            ("Check &file types...", self._check_types),
        ):
            b = wx.Button(self, label=label)
            b.Bind(wx.EVT_BUTTON, handler)
            row.Add(b, 0, wx.RIGHT, 6)
        sizer.Add(row, 0, wx.ALL, 6)
        self.folder = forms.folder_row(self, sizer, "Save &to (leave empty to sort into category folders):", "", settings.recent_folders)
        btns = forms.buttons(
            self, sizer,
            [(wx.ID_OK, "&Start downloads"), (wx.ID_APPLY, "Download &later"), (wx.ID_CANCEL, "Cancel")],
            default=wx.ID_OK,
        )
        self.SetSizer(sizer)
        self.SetSize((900, 560))
        self.CentreOnParent()
        self._check_matching(set(settings.capture_extensions))
        if items:
            self.list.Focus(0)
            self.list.Select(0)
        self.list.SetFocus()
        btns[wx.ID_OK].Bind(wx.EVT_BUTTON, lambda e: self._finish(True))
        btns[wx.ID_APPLY].Bind(wx.EVT_BUTTON, lambda e: self._finish(False))

    def _check_all(self, value: bool):
        for i in range(self.list.GetItemCount()):
            self.list.CheckItem(i, value)

    def _check_matching(self, extensions: set[str]):
        any_checked = False
        for i, dl in enumerate(self.items):
            name = dl.filename or filenames.from_url(dl.url)
            ext = Path(name).suffix.lower().lstrip(".")
            hit = ext in extensions
            self.list.CheckItem(i, hit)
            any_checked |= hit
        if not any_checked and len(self.items) <= 20:
            self._check_all(True)  # a short hand-made list: assume the user wants it all

    def _check_types(self, evt):
        with wx.TextEntryDialog(
            self, "File types to check, separated by spaces (for example: mp3 zip pdf):", "Check file types"
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                wanted = {t.lower().lstrip(".") for t in dlg.GetValue().split()}
                self._check_matching(wanted)
        self.list.SetFocus()

    def _finish(self, start: bool):
        chosen = [dl for i, dl in enumerate(self.items) if self.list.IsItemChecked(i)]
        if not chosen:
            forms.message(self, "Check at least one link, or choose Cancel.", icon=wx.ICON_WARNING)
            self.list.SetFocus()
            return
        folder = self.folder.GetValue().strip()
        for dl in chosen:
            if folder:
                dl.directory = folder
        self.items = chosen
        self.start_now = start
        self.EndModal(wx.ID_OK)


class StreamPickerDialog(wx.Dialog):
    """Choose which of the streams a page played to download (Grab video or
    audio), with the same checkbox list as LinkPickerDialog, which screen
    readers read row by row. yt-dlp on the page stays one button away.

    Afterwards: ``chosen`` (the checked streams), ``start_now``,
    ``folder_path`` and ``use_ytdlp``.
    """

    def __init__(self, parent: wx.Window | None, streams: list, recent: list[str] | None = None):
        super().__init__(parent, title="Grab video or audio", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.streams = streams
        self.chosen: list = []
        self.start_now = True
        self.use_ytdlp = False
        self.folder_path = ""
        sizer = wx.BoxSizer(wx.VERTICAL)
        noun = "stream" if len(streams) == 1 else "streams"
        sizer.Add(wx.StaticText(self, label=f"The page played {len(streams)} audio or video {noun}, newest first. "
                                "&Streams, Space checks or unchecks:"), 0, wx.ALL, 6)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.EnableCheckBoxes(True)
        set_accessible_name(self.list, "Streams")
        self.list.InsertColumn(0, "Stream", width=430)
        self.list.InsertColumn(1, "Address", width=430)
        for i, stream in enumerate(streams):
            parts = urlsplit(stream.url)
            self.list.InsertItem(i, stream.label())
            self.list.SetItem(i, 1, f"{parts.hostname or ''}{parts.path}"[:150])
            self.list.CheckItem(i, True)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, value in (("Check &all", True), ("&Uncheck all", False)):
            b = wx.Button(self, label=label)
            b.Bind(wx.EVT_BUTTON, lambda e, v=value: self._check_all(v))
            row.Add(b, 0, wx.RIGHT, 6)
        sizer.Add(row, 0, wx.ALL, 6)
        self.folder = forms.folder_row(self, sizer, "Save &to (leave empty to sort into category folders):", "", recent)
        btns = forms.buttons(
            self, sizer,
            [(wx.ID_OK, "&Start downloads"), (wx.ID_APPLY, "Download &later"),
             (wx.ID_MORE, "Look on the page with &yt-dlp instead"), (wx.ID_CANCEL, "Cancel")],
            default=wx.ID_OK,
        )
        self.SetSizer(sizer)
        self.SetSize((920, 460))
        self.CentreOnParent()
        if streams:
            self.list.Focus(0)
            self.list.Select(0)
        self.list.SetFocus()
        btns[wx.ID_OK].Bind(wx.EVT_BUTTON, lambda e: self._finish(True))
        btns[wx.ID_APPLY].Bind(wx.EVT_BUTTON, lambda e: self._finish(False))
        btns[wx.ID_MORE].Bind(wx.EVT_BUTTON, self._ytdlp)

    def _check_all(self, value: bool):
        for i in range(self.list.GetItemCount()):
            self.list.CheckItem(i, value)
        self.list.SetFocus()

    def _finish(self, start: bool):
        chosen = [s for i, s in enumerate(self.streams) if self.list.IsItemChecked(i)]
        if not chosen:
            forms.message(self, "Check at least one stream, or choose Cancel.", icon=wx.ICON_WARNING)
            self.list.SetFocus()
            return
        self.chosen, self.start_now = chosen, start
        self.folder_path = self.folder.GetValue().strip()
        self.EndModal(wx.ID_OK)

    def _ytdlp(self, evt):
        self.use_ytdlp = True
        self.EndModal(wx.ID_OK)


class BatchDialog(wx.Dialog):
    """IDM-style batch download: one address with a * wildcard and a range."""

    def __init__(self, parent: wx.Window | None):
        super().__init__(parent, title="Batch download")
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(
            wx.StaticText(self, label="Use * in the address where the number or letter changes, "
                          "for example https://example.com/photos/img*.jpg"),
            0, wx.ALL, 6,
        )
        self.address = forms.text(self, sizer, "&Address with *:", "")
        self.address.SetMinSize((520, -1))
        self.start = forms.text(self, sizer, "&From (a number or a letter):", "1")
        self.end = forms.text(self, sizer, "&To:", "10")
        self.width = forms.spin(self, sizer, "&Wildcard width, padding numbers with zeros (0 for none):", 0, 0, 10)
        forms.buttons(self, sizer, [(wx.ID_OK, "OK"), (wx.ID_CANCEL, "Cancel")], default=wx.ID_OK)
        self.SetSizerAndFit(sizer)
        self.address.SetFocus()
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, evt):
        try:
            self.urls = expand_batch(self.address.GetValue().strip(), self.start.GetValue(), self.end.GetValue(), self.width.GetValue())
        except ValueError as e:
            forms.message(self, str(e), icon=wx.ICON_WARNING)
            return
        evt.Skip()


def expand_batch(pattern: str, start: str, end: str, width: int = 0) -> list[str]:
    if pattern.count("*") != 1:
        raise ValueError("Put exactly one * in the address.")
    if not URL_RE.match(pattern.replace("*", "1")):
        raise ValueError("The address must start with http:// or https://")
    start, end = start.strip(), end.strip()
    if start.isdigit() and end.isdigit():
        lo, hi = int(start), int(end)
        values = [str(n).zfill(width) for n in range(lo, hi + 1)] if lo <= hi else []
    elif len(start) == 1 and len(end) == 1 and start.isalpha() and end.isalpha():
        values = [chr(c) for c in range(ord(start), ord(end) + 1)]
    else:
        raise ValueError("From and To must both be numbers, or both single letters.")
    if not values:
        raise ValueError("From must come before To.")
    if len(values) > 10000:
        raise ValueError("That is more than 10,000 addresses.")
    return [pattern.replace("*", v) for v in values]


class PropertiesDialog(wx.Dialog):
    """Everything about one download, plus the settings that can change."""

    def __init__(self, parent: wx.Window | None, dl: Download, running: bool, recent: list[str] | None = None):
        super().__init__(parent, title=f"Properties of {dl.display_name()}", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dl = dl
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.details = forms.readonly(self, sizer, "&Details:", details_text(dl, running), lines=12)
        self.filename = forms.text(self, sizer, "File &name:", dl.filename)
        self.folder = forms.folder_row(self, sizer, "&Folder:", dl.directory, recent)
        if running:
            for ctrl in (self.filename, self.folder):
                ctrl.Disable()
                ctrl.SetToolTip("Pause the download to rename or move it")
        self.address = forms.text(self, sizer, "&Address (change it to renew an expired link):", dl.url)
        self.limit = forms.text(
            self, sizer, "Speed &limit for this download (for example 500K or 2M; empty for none):",
            format_size(dl.speed_limit).replace(" ", "").replace("bytes", "") if dl.speed_limit else "",
        )
        self.connections = forms.spin(self, sizer, "&Connections (0 uses the default):", dl.connections, 0, 32)
        self.checksum = forms.text(self, sizer, "E&xpected checksum:", dl.checksum)
        self.username = forms.text(self, sizer, "&User name for the server:", dl.username)
        self.password = forms.text(self, sizer, "&Password:", dl.password, style=wx.TE_PASSWORD)
        forms.buttons(self, sizer, [(wx.ID_OK, "OK"), (wx.ID_CANCEL, "Cancel")], default=wx.ID_OK)
        self.SetSizer(sizer)
        self.SetSize((720, 680))
        self.CentreOnParent()
        self.details.SetFocus()
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, evt):
        limit_text = self.limit.GetValue().strip()
        limit = parse_size(limit_text) if limit_text else 0
        if limit is None:
            forms.message(self, "Write the speed limit like 500K or 2M, or leave it empty.", icon=wx.ICON_WARNING)
            self.limit.SetFocus()
            return
        expected = self.checksum.GetValue().strip()
        if expected and checksum_mod.parse(expected) is None:
            forms.message(self, "That does not look like an MD5, SHA-1, SHA-256 or SHA-512 checksum.", icon=wx.ICON_WARNING)
            self.checksum.SetFocus()
            return
        found = urls_in(self.address.GetValue())
        if not found:
            forms.message(self, "The address must start with http:// or https://", icon=wx.ICON_WARNING)
            self.address.SetFocus()
            return
        self.new_url = found[0]
        self.new_name = self.filename.GetValue().strip() if self.filename.IsEnabled() else self.dl.filename
        self.new_folder = self.folder.GetValue().strip() if self.folder.IsEnabled() else self.dl.directory
        if self.filename.IsEnabled() and not self.new_name:
            forms.message(self, "Give the file a name.", icon=wx.ICON_WARNING)
            self.filename.SetFocus()
            return
        self.values = dict(
            speed_limit=limit,
            connections=self.connections.GetValue(),
            checksum=expected,
            username=self.username.GetValue(),
            password=self.password.GetValue(),
        )
        evt.Skip()


def details_text(dl: Download, running: bool) -> str:
    lines = [
        f"Name: {dl.display_name()}",
        f"Status: {status_text(dl)}",
    ]
    size = size_text(dl)
    if size:
        lines.append(f"Size: {size}")
    if running and dl.status == Status.DOWNLOADING:
        if dl.speed:
            lines.append(f"Speed: {format_speed(dl.speed)}")
        if dl.eta is not None:
            lines.append(f"Time left: {format_duration(dl.eta)}")
        lines.append(f"Connections: {dl.active_connections}")
    if dl.resumable is not None:
        lines.append("Can resume: " + ("yes" if dl.resumable else "no, a paused download starts over"))
    if dl.directory:
        lines.append(f"Folder: {dl.directory}")
    lines.append(f"Address: {net.safe_url(dl.url)}")
    if dl.referrer:
        lines.append(f"Referrer: {net.safe_url(dl.referrer)}")
    if dl.mime:
        lines.append(f"Type: {dl.mime}")
    lines.append("Added: " + time.strftime("%Y-%m-%d %H:%M", time.localtime(dl.added_at)))
    if dl.completed_at:
        lines.append("Completed: " + time.strftime("%Y-%m-%d %H:%M", time.localtime(dl.completed_at)))
    if dl.checksum:
        state = {True: "verified", False: "MISMATCH", None: "not checked yet"}[dl.checksum_ok]
        lines.append(f"Checksum: {state}")
    if dl.error:
        lines.append(f"Error: {dl.error}")
    if dl.description:
        lines.append(f"Description: {dl.description}")
    return "\n".join(lines)


class ChecksumDialog(wx.Dialog):
    """Compute a finished file's checksums and compare with a published one."""

    def __init__(self, parent: wx.Window | None, path: str, expected: str = ""):
        super().__init__(parent, title=f"Checksums of {Path(path).name}", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.path = path
        self._digests: dict[str, str] = {}
        self._alive = True
        self._stop = threading.Event()
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.results = forms.readonly(self, sizer, "&Checksums:", "Calculating...", lines=5)
        self.compare = forms.text(self, sizer, "C&ompare with (paste the published checksum):", expected)
        self.verdict = forms.readonly(self, sizer, "&Result:", "")
        btns = forms.buttons(self, sizer, [(wx.ID_COPY, "&Copy SHA-256"), (wx.ID_CLOSE, "Close")], default=wx.ID_CLOSE)
        self.SetSizer(sizer)
        self.SetSize((760, 420))
        self.CentreOnParent()
        self.SetEscapeId(wx.ID_CLOSE)
        self.compare.Bind(wx.EVT_TEXT, lambda e: self._update_verdict())
        btns[wx.ID_COPY].Bind(wx.EVT_BUTTON, self._copy)
        btns[wx.ID_CLOSE].Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.results.SetFocus()
        threading.Thread(target=self._work, daemon=True).start()

    def _on_destroy(self, evt):
        if evt.GetEventObject() is self:
            self._alive = False
            self._stop.set()
        evt.Skip()

    def _work(self):
        try:
            digests = checksum_mod.compute_all(self.path, stop=self._stop)
        except (OSError, InterruptedError) as e:
            wx.CallAfter(self._done, None, str(e))
            return
        wx.CallAfter(self._done, digests, "")

    def _done(self, digests, error):
        if not self._alive:
            return
        if digests is None:
            self.results.SetValue(f"Could not read the file: {error}")
            return
        self._digests = digests
        self.results.SetValue("\n".join(f"{name.upper()}: {value}" for name, value in digests.items()))
        self._update_verdict()

    def _update_verdict(self):
        text = self.compare.GetValue().strip()
        if not text or not self._digests:
            self.verdict.SetValue("")
            return
        parsed = checksum_mod.parse(text)
        if parsed is None:
            self.verdict.SetValue("That is not a checksum Downpour knows.")
        elif self._digests.get(parsed[0]) == parsed[1]:
            self.verdict.SetValue(f"Match. The file is intact ({parsed[0].upper()}).")
        else:
            self.verdict.SetValue(f"NO MATCH. The file differs from the published {parsed[0].upper()} checksum.")

    def _copy(self, evt):
        value = self._digests.get("sha256")
        if value and wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(value))
            wx.TheClipboard.Close()


class CountdownDialog(wx.Dialog):
    """Warn before shutting down or sleeping when the queue is finished."""

    def __init__(self, parent: wx.Window | None, action: str, seconds: int = 60):
        super().__init__(parent, title="Downloads finished")
        self.action = action
        self.remaining = seconds
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.info = forms.readonly(self, sizer, "Status:", self._text())
        self.info.SetMinSize((460, -1))
        btns = forms.buttons(self, sizer, [(wx.ID_OK, "Do it &now"), (wx.ID_CANCEL, "&Cancel")], default=wx.ID_CANCEL)
        self.SetSizerAndFit(sizer)
        self.CentreOnScreen()
        btns[wx.ID_CANCEL].SetFocus()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(1000)

    def _text(self) -> str:
        verb = {"exit": "Downpour will close", "sleep": "The computer will sleep",
                "hibernate": "The computer will hibernate", "shutdown": "The computer will shut down"}[self.action]
        return f"All downloads are finished. {verb} in {self.remaining} seconds."

    def _tick(self, evt):
        self.remaining -= 1
        if self.remaining <= 0:
            self.timer.Stop()
            self.EndModal(wx.ID_OK)
            return
        self.info.SetValue(self._text())
