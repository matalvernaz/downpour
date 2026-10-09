r"""Start Downpour with sample downloads and an open New download dialog,
without taking focus, for inspect_tree.py.

    .venv\Scripts\pythonw.exe tools\a11y\launcher.py [seconds]
"""
from pathlib import Path
import os, sys, tempfile, time
home = tempfile.mkdtemp(prefix="dp-probe-")
os.environ["DOWNPOUR_HOME"] = home
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from downpour.core.paths import data_dir
from downpour.core.store import Store
from downpour.core.models import Download, Status
from downpour.core.segments import Segment

store = Store(data_dir() / "downpour.db")
store.insert(Download(url="https://example.com/files/ubuntu.iso", filename="ubuntu.iso", status=Status.PAUSED,
                      size=5_000_000_000, downloaded=1_250_000_000, segments=[Segment(0, 5_000_000_000, 1_250_000_000)], resumable=True))
store.insert(Download(url="https://example.com/song.mp3", filename="song.mp3", status=Status.COMPLETED, size=4_200_000, downloaded=4_200_000))
store.insert(Download(url="https://example.com/gone.zip", filename="gone.zip", status=Status.FAILED, error="HTTP 404 Not Found - the file is not on the server any more"))
store.close()

import wx
from downpour.gui import app as app_mod
from downpour.gui.dialogs import DownloadInfoDialog
orig_show = app_mod.Controller.show_window

def quiet_show(self):
    self.frame.ShowWithoutActivating()
    self.frame.refresh()
app_mod.Controller.show_window = quiet_show
orig_init = app_mod.Controller.__init__

def init(self, *a, **k):
    orig_init(self, *a, **k)
    def open_dialog():
        d = DownloadInfoDialog(self.frame, self.settings, Download(url="https://example.com/setup.exe", size=12_345_678, filename="setup.exe", filename_fixed=True))
        d.ShowWithoutActivating()
    wx.CallLater(1000, open_dialog)
    wx.CallLater(int(sys.argv[1]) * 1000 if len(sys.argv) > 1 else 20000, self.exit)
app_mod.Controller.__init__ = init
app_mod.run(tray=False)
