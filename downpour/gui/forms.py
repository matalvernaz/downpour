"""Building blocks for accessible dialogs.

Every input gets a visible label created immediately before it (so Windows
names the control after it, and its mnemonic jumps there) and, for the
controls that need it, an explicit accessible name as well - see a11y.py.
"""

from __future__ import annotations

import logging

import wx

from downpour.gui.a11y import set_accessible_name

log = logging.getLogger(__name__)


def plain(label: str) -> str:
    return label.replace("&", "").rstrip(":")


def text(
    parent: wx.Window,
    sizer: wx.Sizer,
    label: str,
    value: str = "",
    style: int = 0,
    proportion: int = 0,
) -> wx.TextCtrl:
    sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
    ctrl = wx.TextCtrl(parent, value=value, style=style)
    set_accessible_name(ctrl, plain(label))
    sizer.Add(ctrl, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
    return ctrl


def readonly(parent: wx.Window, sizer: wx.Sizer, label: str, value: str = "", lines: int = 1) -> wx.TextCtrl:
    """Read-only text the user can tab to and review with the cursor."""
    style = wx.TE_READONLY | (wx.TE_MULTILINE | wx.TE_DONTWRAP if lines > 1 else 0)
    ctrl = text(parent, sizer, label, value, style=style, proportion=1 if lines > 1 else 0)
    if lines > 1:
        ctrl.SetMinSize((-1, ctrl.GetCharHeight() * (lines + 1)))
    return ctrl


def choice(parent: wx.Window, sizer: wx.Sizer, label: str, options: list[str], selected: int = 0) -> wx.Choice:
    sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
    ctrl = wx.Choice(parent, choices=options)
    if options:
        ctrl.SetSelection(max(0, min(selected, len(options) - 1)))
    sizer.Add(ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
    return ctrl


def spin(parent: wx.Window, sizer: wx.Sizer, label: str, value: int, lo: int, hi: int) -> wx.SpinCtrl:
    sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
    ctrl = wx.SpinCtrl(parent, min=lo, max=hi, initial=value)
    sizer.Add(ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
    return ctrl


def check(parent: wx.Window, sizer: wx.Sizer, label: str, value: bool) -> wx.CheckBox:
    ctrl = wx.CheckBox(parent, label=label)
    ctrl.SetValue(value)
    sizer.Add(ctrl, 0, wx.ALL, 6)
    return ctrl


def folder_row(parent: wx.Window, sizer: wx.Sizer, label: str, value: str, recent: list[str] | None = None) -> wx.ComboBox:
    """A folder field with a Browse button after it. The field is a combo box
    whose list holds the recent folders (Alt+Down opens it); typing works as
    in a plain field."""
    sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
    row = wx.BoxSizer(wx.HORIZONTAL)
    ctrl = wx.ComboBox(parent, value=value, choices=list(recent or []), style=wx.CB_DROPDOWN)
    set_accessible_name(ctrl, plain(label))
    row.Add(ctrl, 1, wx.EXPAND | wx.RIGHT, 6)
    browse = wx.Button(parent, label="&Browse...")
    row.Add(browse, 0)
    sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    def on_browse(_evt):
        with wx.DirDialog(parent, "Choose a folder", defaultPath=ctrl.GetValue()) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                ctrl.SetValue(dlg.GetPath())
                ctrl.SetFocus()

    browse.Bind(wx.EVT_BUTTON, on_browse)
    return ctrl


def file_row(parent: wx.Window, sizer: wx.Sizer, label: str, value: str, wildcard: str = "All files|*.*") -> wx.TextCtrl:
    """A file field with a Browse button after it."""
    sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
    row = wx.BoxSizer(wx.HORIZONTAL)
    ctrl = wx.TextCtrl(parent, value=value)
    set_accessible_name(ctrl, plain(label))
    row.Add(ctrl, 1, wx.EXPAND | wx.RIGHT, 6)
    browse = wx.Button(parent, label="Browse...")
    row.Add(browse, 0)
    sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    def on_browse(_evt):
        with wx.FileDialog(parent, "Choose a file", wildcard=wildcard, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                ctrl.SetValue(dlg.GetPath())
                ctrl.SetFocus()

    browse.Bind(wx.EVT_BUTTON, on_browse)
    return ctrl


def buttons(parent: wx.Window, sizer: wx.Sizer, specs: list[tuple[int, str]], default: int | None = None) -> dict[int, wx.Button]:
    """A right-aligned row of buttons. ``specs`` are (id, label) pairs."""
    row = wx.BoxSizer(wx.HORIZONTAL)
    row.AddStretchSpacer()
    out = {}
    for wid, label in specs:
        btn = wx.Button(parent, wid, label)
        row.Add(btn, 0, wx.LEFT, 6)
        out[wid] = btn
    if default is not None and default in out:
        out[default].SetDefault()
    sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
    return out


def message(parent: wx.Window | None, text_: str, title: str = "Downpour", icon: int = wx.ICON_INFORMATION) -> None:
    # Every problem the user was told about belongs in the log too.
    if icon & wx.ICON_ERROR:
        log.error("told the user (%s): %s", title, text_)
    elif icon & wx.ICON_WARNING:
        log.warning("told the user (%s): %s", title, text_)
    else:
        log.debug("told the user (%s): %s", title, text_)
    wx.MessageBox(text_, title, wx.OK | icon, parent)


def confirm(parent: wx.Window | None, text_: str, title: str = "Downpour") -> bool:
    answer = wx.MessageBox(text_, title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, parent) == wx.YES
    log.info("asked the user (%s): %s -> %s", title, text_, "yes" if answer else "no")
    return answer
