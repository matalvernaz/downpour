"""The main window: a filter list, the download list, menus and a status bar.

Everything is reachable from the keyboard. Menus carry the shortcuts, the
download list adds Enter (open), Space (pause or resume), Delete (remove) and
the Applications key (context menu), and the status bar (NVDA+End) always
holds a one-line summary of what is running.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import wx

from downpour.core.models import ACTIVE, Download, Status
from downpour.core.units import format_speed
from downpour.gui import forms
from downpour.gui.a11y import set_accessible_name
from downpour.gui.download_list import DownloadList, summary

if TYPE_CHECKING:
    from downpour.gui.app import Controller

SPEED_PRESETS = [0, 256 * 1024, 512 * 1024, 1024**2, 2 * 1024**2, 5 * 1024**2, 10 * 1024**2]
WHEN_DONE = [("nothing", "Do nothing"), ("exit", "Close Downpour"), ("sleep", "Sleep"), ("hibernate", "Hibernate"), ("shutdown", "Shut down")]

SHORTCUTS = """Downpour keyboard shortcuts

In the download list
Enter: open a finished file, or show the properties of an unfinished one
Space: pause or resume
Delete: remove from the list (the file stays)
Shift+Delete: remove and delete the file
Applications key or Shift+F10: actions for the selected downloads
Ctrl+I: speak the progress of the selected download

Anywhere in the main window
Ctrl+N: add a download
Ctrl+Shift+V: add every link on the clipboard
Ctrl+B: batch download
Ctrl+O: open the file
Ctrl+E: open the containing folder
Ctrl+R: resume
Ctrl+P: pause
Ctrl+Shift+R: resume all
Ctrl+Shift+P: pause all
Ctrl+Up and Ctrl+Down: move in the queue
Ctrl+C: copy the address
Ctrl+K: checksums of the finished file
Alt+Enter: properties
Ctrl+1 to Ctrl+6: show all, running, queued, unfinished, finished or failed downloads
Alt+W: the Show list; Alt+L: the download list
Ctrl+Shift+O: options
Ctrl+Q: exit Downpour completely
F1: this list
"""


class Filter:
    def __init__(self, label: str, test: Callable[[Download], bool]):
        self.label = label
        self.test = test


def build_filters(controller: "Controller") -> list[Filter]:
    s = controller.settings
    out = [
        Filter("All downloads", lambda d: True),
        Filter("Running", lambda d: d.status in ACTIVE),
        Filter("Queued", lambda d: d.status == Status.QUEUED),
        Filter("Unfinished", lambda d: d.status != Status.COMPLETED),
        Filter("Finished", lambda d: d.status == Status.COMPLETED),
        Filter("Failed", lambda d: d.status == Status.FAILED),
    ]
    def in_category(d: Download, name: str) -> bool:
        cat = s.category_for(d.filename)
        return cat is not None and cat.name == name

    for cat in s.categories:
        out.append(Filter(cat.name, lambda d, n=cat.name: in_category(d, n)))
    out.append(Filter("Other files", lambda d: s.category_for(d.filename) is None))
    return out


class MainFrame(wx.Frame):
    def __init__(self, controller: "Controller"):
        super().__init__(None, title="Downpour", size=(1100, 640))
        self.ctl = controller
        self.engine = controller.engine
        self.settings = controller.settings
        self.exiting = False
        self.filters = build_filters(controller)

        panel = wx.Panel(self, name="")  # otherwise exposed to screen readers as "panel"
        outer = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(panel, label="Sho&w:"), 0, wx.ALL, 6)
        self.filter_list = wx.ListBox(panel, choices=[f.label for f in self.filters], style=wx.LB_SINGLE)
        self.filter_list.SetSelection(0)
        left.Add(self.filter_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        outer.Add(left, 0, wx.EXPAND)
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(wx.StaticText(panel, label="Download &list:"), 0, wx.ALL, 6)
        self.list = DownloadList(panel)
        right.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        outer.Add(right, 1, wx.EXPAND)
        panel.SetSizer(outer)
        self.status = self.CreateStatusBar()

        self._build_menus()
        self.filter_list.Bind(wx.EVT_LISTBOX, lambda e: self.refresh())
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)
        self.list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.refresh(), self.timer)
        self.timer.Start(500)
        self.refresh()
        self.list.SetFocus()

    # -- menus ----------------------------------------------------------------

    def _item(self, menu: wx.Menu, label: str, handler, kind=wx.ITEM_NORMAL) -> wx.MenuItem:
        item = menu.Append(wx.ID_ANY, label, kind=kind)
        self.Bind(wx.EVT_MENU, handler, item)
        return item

    def _build_menus(self):
        bar = wx.MenuBar()
        c = self.ctl

        tasks = wx.Menu()
        self._item(tasks, "&Add download...\tCtrl+N", lambda e: c.add_url_dialog())
        self._item(tasks, "Add links from the &clipboard\tCtrl+Shift+V", lambda e: c.add_from_clipboard())
        self._item(tasks, "&Batch download...\tCtrl+B", lambda e: c.batch_dialog())
        self._item(tasks, "&Import links from a text file...", lambda e: c.import_links())
        tasks.AppendSeparator()
        self._item(tasks, "&Hide to the notification area", lambda e: self.Hide())
        self._item(tasks, "E&xit Downpour\tCtrl+Q", lambda e: c.exit())
        bar.Append(tasks, "&Tasks")

        dls = wx.Menu()
        self._item(dls, "&Open file\tCtrl+O", lambda e: self.open_file())
        self._item(dls, "Open containing &folder\tCtrl+E", lambda e: self.open_folder())
        dls.AppendSeparator()
        self._item(dls, "&Resume\tCtrl+R", lambda e: self.resume())
        self._item(dls, "&Pause\tCtrl+P", lambda e: self.pause())
        self._item(dls, "Resume a&ll\tCtrl+Shift+R", lambda e: self.engine.start_all())
        self._item(dls, "Pause all\tCtrl+Shift+P", lambda e: self.engine.pause_all())
        dls.AppendSeparator()
        self._item(dls, "Move &up in the queue\tCtrl+Up", lambda e: self.move(-1))
        self._item(dls, "Move &down in the queue\tCtrl+Down", lambda e: self.move(1))
        dls.AppendSeparator()
        self._item(dls, "Re&move from the list\tDelete", lambda e: self.remove(False))
        self._item(dls, "Remove and &delete the file\tShift+Delete", lambda e: self.remove(True))
        self._item(dls, "Download a&gain", lambda e: self.redownload())
        self._item(dls, "Clear finished downloads from the list", lambda e: self.clear_finished())
        dls.AppendSeparator()
        self._item(dls, "&Copy address\tCtrl+C", lambda e: self.copy_address())
        self._item(dls, "Chec&ksums...\tCtrl+K", lambda e: self.checksums())
        self._item(dls, "Speak &progress\tCtrl+I", lambda e: self.speak_progress())
        self._item(dls, "Propert&ies\tAlt+Enter", lambda e: self.properties())
        bar.Append(dls, "&Downloads")

        view = wx.Menu()
        for i, f in enumerate(self.filters[:6]):
            self._item(view, f"{f.label}\tCtrl+{i + 1}", lambda e, i=i: self.set_filter(i))
        bar.Append(view, "&View")

        opts = wx.Menu()
        self._item(opts, "&Options...\tCtrl+Shift+O", lambda e: c.options_dialog())
        speed = wx.Menu()
        self.speed_items = []
        for rate in SPEED_PRESETS:
            label = "&No limit" if not rate else format_speed(rate)
            item = self._item(speed, label, lambda e, r=rate: c.set_speed_limit(r), kind=wx.ITEM_RADIO)
            self.speed_items.append((rate, item))
        self.speed_custom = self._item(speed, "&Custom...", lambda e: c.custom_speed_limit(), kind=wx.ITEM_RADIO)
        opts.AppendSubMenu(speed, "&Speed limit")
        self.clipboard_item = self._item(opts, "Watch the clip&board", lambda e: c.toggle_clipboard(), kind=wx.ITEM_CHECK)
        done = wx.Menu()
        self.done_items = []
        for key, label in WHEN_DONE:
            item = self._item(done, label, lambda e, k=key: c.set_when_done(k), kind=wx.ITEM_RADIO)
            self.done_items.append((key, item))
        opts.AppendSubMenu(done, "When all downloads &finish")
        self._item(opts, "Browser &integration...", lambda e: c.options_dialog(page=3))
        bar.Append(opts, "&Options")

        help_ = wx.Menu()
        self._item(help_, "&Keyboard shortcuts\tF1", lambda e: self.show_shortcuts())
        self._item(help_, "&About Downpour", lambda e: c.about())
        bar.Append(help_, "&Help")
        self.SetMenuBar(bar)
        self.Bind(wx.EVT_MENU_OPEN, self._sync_menu_state)

    def _sync_menu_state(self, evt):
        s = self.settings
        limit = s.effective_speed_limit()
        matched = False
        for rate, item in self.speed_items:
            if rate == limit:
                item.Check(True)
                matched = True
        if not matched:
            self.speed_custom.Check(True)
        self.clipboard_item.Check(s.clipboard_monitor)
        for key, item in self.done_items:
            if key == s.when_done:
                item.Check(True)
        evt.Skip()

    # -- refresh --------------------------------------------------------------

    def current_filter(self) -> Filter:
        i = self.filter_list.GetSelection()
        return self.filters[i if 0 <= i < len(self.filters) else 0]

    def rebuild_filters(self) -> None:
        """Categories may have changed in Options; keep the user's place."""
        label = self.current_filter().label
        self.filters = build_filters(self.ctl)
        labels = [f.label for f in self.filters]
        self.filter_list.Set(labels)
        self.filter_list.SetSelection(labels.index(label) if label in labels else 0)
        self.refresh()

    def set_filter(self, index: int) -> None:
        self.filter_list.SetSelection(index)
        self.refresh()
        self.list.SetFocus()

    def refresh(self) -> None:
        if not self.IsShown():
            return
        test = self.current_filter().test
        self.list.set_rows([d for d in self.engine.downloads() if test(d)])
        running, queued, speed = self.engine.totals()
        if running:
            text = f"{running} downloading at {format_speed(speed) or '0 bytes/s'}"
        else:
            text = "Nothing downloading"
        if queued:
            text += f", {queued} queued"
        limit = self.settings.effective_speed_limit()
        if limit:
            text += f", speed limited to {format_speed(limit)}"
        if self.status.GetStatusText() != text:
            self.status.SetStatusText(text)

    # -- actions on the selection -----------------------------------------------

    def _targets(self) -> list[Download]:
        chosen = self.list.targets()
        if not chosen:
            wx.Bell()
        return chosen

    def open_file(self):
        for dl in self._targets()[:1]:
            self.ctl.open_download(dl)

    def open_folder(self):
        for dl in self._targets()[:1]:
            self.ctl.open_folder(dl)

    def resume(self):
        self.engine.start([d.id for d in self._targets()])

    def pause(self):
        self.engine.pause([d.id for d in self._targets()])

    def toggle(self):
        chosen = self._targets()
        if not chosen:
            return
        if any(d.status in ACTIVE or d.status == Status.QUEUED for d in chosen):
            self.engine.pause([d.id for d in chosen])
            self.ctl.feedback.say("Pausing")
        else:
            self.engine.start([d.id for d in chosen])
            self.ctl.feedback.say("Resuming")

    def move(self, offset: int):
        chosen = self._targets()
        if chosen:
            focus = self.list.focused()
            self.engine.move([d.id for d in chosen], offset)
            self.refresh()
            self.list.select_ids({d.id for d in chosen}, focus.id if focus else None)

    def remove(self, delete_files: bool):
        chosen = self._targets()
        if not chosen:
            return
        what = chosen[0].display_name() if len(chosen) == 1 else f"{len(chosen)} downloads"
        if delete_files:
            question = f"Remove {what} from the list and delete the file from your computer?"
        else:
            question = f"Remove {what} from the list? Finished files stay on your computer."
        if forms.confirm(self, question):
            self.engine.remove([d.id for d in chosen], delete_files=delete_files)

    def redownload(self):
        chosen = self._targets()[:1]
        if chosen and forms.confirm(self, f"Download {chosen[0].display_name()} again from the start? Any copy already downloaded is replaced."):
            self.engine.redownload(chosen[0].id)

    def clear_finished(self):
        done = [d.id for d in self.engine.downloads() if d.status == Status.COMPLETED]
        if done and forms.confirm(self, f"Remove {len(done)} finished downloads from the list? The files stay on your computer."):
            self.engine.remove(done)

    def copy_address(self):
        chosen = self._targets()
        if chosen:
            self.ctl.copy_text("\n".join(d.url for d in chosen))
            self.ctl.feedback.say("Address copied" if len(chosen) == 1 else f"{len(chosen)} addresses copied")

    def checksums(self):
        for dl in self._targets()[:1]:
            self.ctl.checksum_dialog(dl)

    def speak_progress(self):
        dl = self.list.focused()
        if dl is None:
            return
        text = summary(dl)
        if not self.ctl.feedback.say(text, interrupt=True):
            self.status.SetStatusText(text)

    def properties(self):
        for dl in self._targets()[:1]:
            self.ctl.properties_dialog(dl)

    def show_shortcuts(self):
        with wx.Dialog(self, title="Keyboard shortcuts", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER) as dlg:
            sizer = wx.BoxSizer(wx.VERTICAL)
            text = forms.readonly(dlg, sizer, "Shortcuts:", SHORTCUTS, lines=20)
            forms.buttons(dlg, sizer, [(wx.ID_CLOSE, "Close")], default=wx.ID_CLOSE)
            dlg.SetEscapeId(wx.ID_CLOSE)
            dlg.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
            dlg.SetSizer(sizer)
            dlg.SetSize((620, 560))
            dlg.CentreOnParent()
            text.SetFocus()
            dlg.ShowModal()

    # -- events ---------------------------------------------------------------

    def _on_activate(self, evt):
        dl = self.list.focused()
        if dl is None:
            return
        if dl.status == Status.COMPLETED:
            self.ctl.open_download(dl)
        else:
            self.ctl.properties_dialog(dl)

    def _on_list_key(self, evt):
        key = evt.GetKeyCode()
        if key == wx.WXK_SPACE and not evt.HasAnyModifiers():
            self.toggle()
            return
        if key == wx.WXK_RETURN and evt.AltDown():
            self.properties()
            return
        evt.Skip()

    def _on_context_menu(self, evt):
        menu = wx.Menu()
        entries = [
            ("Open file", self.open_file),
            ("Open containing folder", self.open_folder),
            (None, None),
            ("Resume", self.resume),
            ("Pause", self.pause),
            ("Move up in the queue", lambda: self.move(-1)),
            ("Move down in the queue", lambda: self.move(1)),
            (None, None),
            ("Remove from the list", lambda: self.remove(False)),
            ("Remove and delete the file", lambda: self.remove(True)),
            ("Download again", self.redownload),
            (None, None),
            ("Copy address", self.copy_address),
            ("Checksums...", self.checksums),
            ("Properties", self.properties),
        ]
        for label, handler in entries:
            if label is None:
                menu.AppendSeparator()
                continue
            item = menu.Append(wx.ID_ANY, label)
            self.Bind(wx.EVT_MENU, lambda e, h=handler: h(), item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_close(self, evt):
        if self.exiting or not self.settings.close_to_tray or not evt.CanVeto():
            self.ctl.exit()
            return
        evt.Veto()
        self.Hide()
        self.ctl.feedback.say("Downpour is still running in the notification area")
