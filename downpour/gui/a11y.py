"""Accessibility helpers, derived from measurement rather than folklore.

Carried over from CleanSweep, where everything here was verified against the
live MSAA tree on Windows 11 with wxPython 4.3.1 / wxWidgets 3.3.3 by launching
each control and reading back what the accessibility layer exposed. The
scripts that took Downpour's own measurements are in tools/a11y. The
findings:

* `wx.CheckBox` and `wx.Button` expose their *label* as the accessible name.
  They need no help.

* `wx.ListCtrl` and `wx.TextCtrl` expose an EMPTY name. The `name=` keyword
  argument to the constructor does NOT reach the accessibility layer - this is
  wxWidgets/Phoenix issue #1129. A screen reader announces a bare "list" or
  "edit" with no indication of what it holds. Both must be labelled explicitly.

* Two techniques work. A `wx.StaticText` placed immediately before a control
  is picked up as that control's name (the old Win32 dialog convention). A
  custom `wx.Accessible` overriding `GetName` also works. When both are
  present, `SetAccessible` wins.

* `wx.StaticBox` becomes a real MSAA Group (role 20) with its label as the
  group name, and its children nest correctly inside it - but only if those
  children are parented to the StaticBox itself, not to the surrounding panel.
  Parenting them to the panel raises an assertion in wxWidgets 3.3.

* `wx.ListCtrl` with `EnableCheckBoxes(True)` is a native SysListView32. It
  exposes every row as a ListItem carrying the CHECKED state, with one Text
  child per column. This is the accessible way to present a checkable list.

* `wx.CheckListBox` exposes NO children at all - a screen reader sees an empty
  list. It is unusable and must never appear in this program.

* Measured in Downpour: a virtual ``wx.ListCtrl`` (LC_VIRTUAL) refreshed
  twice a second during a live download raised no WinEvents at all in nine
  seconds, so changing progress figures cannot make a screen reader chatter.
  Its rows still expose a name plus one Text child per column.

* Measured in Downpour: a custom Accessible must answer GetName only for
  childId 0. Answering for every child renamed all items of a ListBox to the
  box's own name. A ListBox is named by the StaticText before it anyway.
"""

from __future__ import annotations

import wx


class _NamedAccessible(wx.Accessible):
    """Reports a fixed accessible name for a control that has none."""

    def __init__(self, window: wx.Window, name: str):
        super().__init__(window)
        self._name = name

    def GetName(self, childId):  # noqa: N802 - wx API casing
        # Only the control itself. Answering for children too renamed every
        # item of a ListBox to the box's own name (measured in Downpour).
        if childId != wx.ACC_SELF:
            return (wx.ACC_NOT_IMPLEMENTED, "")
        return (wx.ACC_OK, self._name)


def set_accessible_name(window: wx.Window, name: str) -> None:
    """Give a control a real accessible name.

    Use for every ListCtrl and TextCtrl. The reference is stashed on the
    window so Python cannot garbage-collect the Accessible out from under the
    C++ side.
    """
    accessible = _NamedAccessible(window, name)
    window._downpour_accessible = accessible  # keep alive
    window.SetAccessible(accessible)


def labelled_text(
    parent: wx.Window,
    sizer: wx.Sizer,
    label: str,
    *,
    value: str = "",
    proportion: int = 1,
    style: int = wx.TE_MULTILINE | wx.TE_READONLY,
) -> wx.TextCtrl:
    """A read-only multiline text pane with a visible and an accessible label.

    A focusable read-only TextCtrl is the right home for output in an
    accessible Windows app: the screen reader reads it on focus, arrow keys
    move line by line, and the review cursor works normally. wx has no binding
    for UIA notification events, so moving focus here is how a completed scan
    announces itself.
    """
    static = wx.StaticText(parent, label=label)
    sizer.Add(static, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
    ctrl = wx.TextCtrl(parent, value=value, style=style)
    set_accessible_name(ctrl, label.replace("&", ""))
    sizer.Add(ctrl, proportion, wx.EXPAND | wx.ALL, 6)
    return ctrl


def speak_via_focus(window: wx.Window) -> None:
    """Make a screen reader announce a control by moving focus to it.

    The honest name for what this is: a workaround. There is no wx binding for
    UIA's notification event, and a StaticText that changes is never announced
    because it cannot hold focus. Moving focus to the results pane is what
    accessible Windows applications actually do.
    """
    if window:
        window.SetFocus()
        if isinstance(window, wx.TextCtrl):
            window.SetInsertionPoint(0)
