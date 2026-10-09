"""The main list of downloads.

A virtual report-mode ListCtrl: the control asks for cell text only when it
paints or when the screen reader queries a row, so thousands of finished
downloads cost nothing, and progress repaints do not raise accessibility
events that would make NVDA chatter while the user is reading.

Column order is chosen for listening: name first, then a status that already
contains the percentage, then size, speed and time left.
"""

from __future__ import annotations

import time

import wx

from downpour.core.models import Download, Status
from downpour.core.units import format_duration, format_size, format_speed
from downpour.gui.a11y import set_accessible_name

COLUMNS = (
    ("Name", 280),
    ("Status", 170),
    ("Size", 170),
    ("Speed", 100),
    ("Time left", 150),
    ("Added", 140),
)


def status_text(dl: Download) -> str:
    pct = dl.progress
    percent = f" {int(pct * 100)}%" if pct is not None and dl.downloaded else ""
    s = dl.status
    if s == Status.DOWNLOADING:
        return "Downloading" + percent
    if s == Status.PAUSED:
        return "Paused" + percent
    if s == Status.QUEUED:
        return "Queued" + percent
    if s == Status.FAILED:
        return f"Failed: {dl.error}" if dl.error else "Failed"
    return s.label


def size_text(dl: Download) -> str:
    if dl.status == Status.COMPLETED:
        return format_size(dl.size)
    if dl.size:
        if dl.downloaded:
            return f"{format_size(dl.downloaded)} of {format_size(dl.size)}"
        return format_size(dl.size)
    if dl.downloaded:
        return format_size(dl.downloaded)
    return ""


def cell(dl: Download, col: int) -> str:
    if col == 0:
        return dl.display_name()
    if col == 1:
        return status_text(dl)
    if col == 2:
        return size_text(dl)
    running = dl.status == Status.DOWNLOADING
    if col == 3:
        return format_speed(dl.speed) if running else ""
    if col == 4:
        return format_duration(dl.eta) if running and dl.eta is not None else ""
    if col == 5:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(dl.added_at))
    return ""


def summary(dl: Download) -> str:
    """One sentence about a download, for speaking on request."""
    parts = [dl.display_name(), status_text(dl)]
    size = size_text(dl)
    if size:
        parts.append(size)
    if dl.status == Status.DOWNLOADING:
        if dl.speed:
            parts.append(format_speed(dl.speed))
        if dl.eta is not None:
            parts.append(f"{format_duration(dl.eta)} left")
    return ", ".join(parts)


class DownloadList(wx.ListCtrl):
    def __init__(self, parent: wx.Window):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        for i, (title, width) in enumerate(COLUMNS):
            self.InsertColumn(i, title, width=width)
        set_accessible_name(self, "Downloads")
        self.rows: list[Download] = []
        self.SetItemCount(0)

    def OnGetItemText(self, item: int, col: int) -> str:  # noqa: N802 - wx API
        try:
            return cell(self.rows[item], col)
        except IndexError:
            return ""

    # -- selection ------------------------------------------------------------

    def selected_indices(self) -> list[int]:
        out = []
        i = self.GetFirstSelected()
        while i != -1:
            out.append(i)
            i = self.GetNextSelected(i)
        return out

    def selected(self) -> list[Download]:
        rows = self.rows
        return [rows[i] for i in self.selected_indices() if i < len(rows)]

    def focused(self) -> Download | None:
        i = self.GetFocusedItem()
        if 0 <= i < len(self.rows):
            return self.rows[i]
        return None

    def targets(self) -> list[Download]:
        """What a command should act on: the selection, else the focused row."""
        chosen = self.selected()
        if chosen:
            return chosen
        dl = self.focused()
        return [dl] if dl else []

    def select_ids(self, ids: set[int], focus_id: int | None = None) -> None:
        for i in self.selected_indices():
            self.Select(i, False)
        for i, dl in enumerate(self.rows):
            if dl.id in ids:
                self.Select(i, True)
        if focus_id is not None:
            for i, dl in enumerate(self.rows):
                if dl.id == focus_id:
                    self.Focus(i)
                    break

    # -- refreshing -----------------------------------------------------------

    def set_rows(self, rows: list[Download]) -> None:
        """Show ``rows``, keeping selection and focus on the same downloads."""
        if [d.id for d in rows] == [d.id for d in self.rows]:
            self.rows = rows
            if rows:
                self.RefreshItems(0, len(rows) - 1)
            return
        selected_ids = {d.id for d in self.selected()}
        old_focus_index = self.GetFocusedItem()
        focus = self.focused()
        self.rows = rows
        for i in self.selected_indices():
            self.Select(i, False)
        self.SetItemCount(len(rows))
        new_index = {d.id: i for i, d in enumerate(rows)}
        for dl_id in selected_ids:
            if dl_id in new_index:
                self.Select(new_index[dl_id], True)
        if rows:
            if focus is not None and focus.id in new_index:
                target = new_index[focus.id]
            else:
                # The focused download went away: land on its neighbour.
                target = min(max(old_focus_index, 0), len(rows) - 1)
                if not selected_ids & set(new_index):
                    self.Select(target, True)
            if target != old_focus_index or focus is None or focus.id not in new_index:
                self.Focus(target)
            self.RefreshItems(0, len(rows) - 1)
        self.Refresh()
