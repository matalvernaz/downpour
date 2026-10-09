"""How Downpour tells the user that something happened.

Three channels, chosen so a screen-reader user hears each event once:

* Speech through the user's own running NVDA (its voice, rate and verbosity)
  via NV Access's controller client library.
* Short sounds, which work with any screen reader or none.
* Windows notifications - used only when speech was not delivered, because
  NVDA also reads notifications aloud and the user would hear it twice.
"""

from __future__ import annotations

import ctypes
import logging
import sys

import wx
import wx.adv

from downpour.core.paths import resource_dir
from downpour.core.settings import Settings

log = logging.getLogger(__name__)


class Speech:
    def __init__(self):
        self._dll = None
        if sys.platform != "win32":
            return
        path = resource_dir() / "vendor" / "nvdaControllerClient.dll"
        try:
            dll = ctypes.WinDLL(str(path))
        except OSError:
            log.info("NVDA controller client not found at %s; speech disabled", path)
            return
        dll.nvdaController_speakText.argtypes = [ctypes.c_wchar_p]
        dll.nvdaController_speakText.restype = ctypes.c_ulong
        dll.nvdaController_cancelSpeech.argtypes = []
        dll.nvdaController_cancelSpeech.restype = ctypes.c_ulong
        dll.nvdaController_testIfRunning.argtypes = []
        dll.nvdaController_testIfRunning.restype = ctypes.c_ulong
        self._dll = dll

    @property
    def available(self) -> bool:
        return self._dll is not None and self._dll.nvdaController_testIfRunning() == 0

    def speak(self, text: str, interrupt: bool = False) -> bool:
        """Speak through NVDA. False when NVDA is not running."""
        if self._dll is None:
            return False
        try:
            if interrupt:
                self._dll.nvdaController_cancelSpeech()
            return self._dll.nvdaController_speakText(text) == 0
        except OSError:
            return False


class Feedback:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.speech = Speech()
        self._taskbar_icon: wx.adv.TaskBarIcon | None = None

    def use_taskbar_icon(self, icon: wx.adv.TaskBarIcon) -> None:
        # Balloon notifications from the tray icon become Windows toasts, and
        # need no Start-menu shortcut or app ID, unlike native toasts.
        self._taskbar_icon = icon
        wx.adv.NotificationMessage.UseTaskBarIcon(icon)

    def play(self, name: str) -> None:
        if not self.settings.sound_complete or sys.platform != "win32":
            return
        import winsound

        path = resource_dir() / "sounds" / f"{name}.wav"
        try:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except RuntimeError:
            log.debug("could not play %s", path)

    def say(self, text: str, interrupt: bool = False) -> bool:
        """Speak if the user wants spoken announcements. True if spoken."""
        if not self.settings.speak_events:
            return False
        return self.speech.speak(text, interrupt)

    def announce(self, title: str, message: str, sound: str | None = None, spoken: str | None = None) -> None:
        if sound:
            self.play(sound)
        if self.say(spoken or f"{title}. {message}"):
            return
        if self.settings.notify_complete:
            note = wx.adv.NotificationMessage(title, message)
            note.Show(timeout=wx.adv.NotificationMessage.Timeout_Auto)
