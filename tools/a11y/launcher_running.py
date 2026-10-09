r"""Start Downpour with one slow live download, without taking focus, for
hook_events.py.

    .venv\Scripts\pythonw.exe tools\a11y\launcher_running.py
"""
from pathlib import Path
import os, sys, tempfile
home = tempfile.mkdtemp(prefix="dp-events-")
os.environ["DOWNPOUR_HOME"] = home
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import wx
from downpour.core.models import Download
from downpour.gui import app as app_mod
from tests.helpers import Server, payload

srv = Server()
url = srv.add("/slow.bin", payload(8_000_000), delay=0.25)

def quiet_show(self):
    self.frame.ShowWithoutActivating()
    self.frame.refresh()
app_mod.Controller.show_window = quiet_show
orig_init = app_mod.Controller.__init__
def init(self, *a, **k):
    orig_init(self, *a, **k)
    self.settings.download_dir = os.path.join(home, "dl")
    self.settings.speak_events = False
    self.settings.sound_complete = False
    self.settings.notify_complete = False
    def go():
        self.engine.add(Download(url=url), force=True)
        self.frame.refresh()
        self.frame.list.Focus(0); self.frame.list.Select(0)
    wx.CallLater(500, go)
    wx.CallLater(14000, self.exit)
app_mod.Controller.__init__ = init
app_mod.run(tray=False)
srv.close()
