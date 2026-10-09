"""Notification-area icon and the app icon.

Reach it from the keyboard with Windows+B, then the arrow keys: Enter opens
Downpour, the Applications key opens its menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import wx
import wx.adv

if TYPE_CHECKING:
    from downpour.gui.app import Controller


def make_icon(size: int = 32) -> wx.Icon:
    """Draw the icon in code - a white arrow on a blue drop - so no image
    files need shipping."""
    bmp = wx.Bitmap(size, size, 32)
    bmp.UseAlpha()
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(0, 0, 0, 0)))
    dc.Clear()
    gc = wx.GraphicsContext.Create(dc)
    s = size / 32
    gc.SetBrush(wx.Brush(wx.Colour(25, 103, 210)))
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.DrawEllipse(1 * s, 1 * s, 30 * s, 30 * s)
    gc.SetBrush(wx.Brush(wx.Colour(255, 255, 255)))
    path = gc.CreatePath()
    path.MoveToPoint(13 * s, 6 * s)
    path.AddLineToPoint(19 * s, 6 * s)
    path.AddLineToPoint(19 * s, 16 * s)
    path.AddLineToPoint(24 * s, 16 * s)
    path.AddLineToPoint(16 * s, 26 * s)
    path.AddLineToPoint(8 * s, 16 * s)
    path.AddLineToPoint(13 * s, 16 * s)
    path.CloseSubpath()
    gc.FillPath(path)
    del gc
    dc.SelectObject(wx.NullBitmap)
    icon = wx.Icon()
    icon.CopyFromBitmap(bmp)
    return icon


class TrayIcon(wx.adv.TaskBarIcon):
    def __init__(self, controller: "Controller"):
        super().__init__()
        self.ctl = controller
        self._icon = make_icon()
        self._tooltip = "Downpour"
        self.SetIcon(self._icon, self._tooltip)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, lambda e: controller.show_window())
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, lambda e: controller.show_window())

    def set_tooltip(self, text: str) -> None:
        if text != self._tooltip:
            self._tooltip = text
            self.SetIcon(self._icon, text)

    def CreatePopupMenu(self) -> wx.Menu:  # noqa: N802 - wx API
        c = self.ctl
        menu = wx.Menu()
        for label, handler in (
            ("&Open Downpour", c.show_window),
            ("&Add download...", c.add_url_dialog),
            ("Resume a&ll", c.engine.start_all),
            ("&Pause all", c.engine.pause_all),
            (None, None),
            ("E&xit", c.exit),
        ):
            if label is None:
                menu.AppendSeparator()
                continue
            item = menu.Append(wx.ID_ANY, label)
            self.Bind(wx.EVT_MENU, lambda e, h=handler: h(), item)
        return menu
