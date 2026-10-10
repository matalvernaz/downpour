"""Command-line commands that installers run: autostart and quit."""

from __future__ import annotations

import json
import subprocess
import sys
import time

from downpour.cli.main import main
from downpour.core import ipc, winutil


def test_autostart_changes_the_setting_and_the_run_key_together(isolated_home, monkeypatch):
    calls = []
    monkeypatch.setattr(winutil, "set_start_with_windows", lambda on, command: calls.append((on, command)))
    assert main(["autostart", "on"]) == 0
    assert json.loads((isolated_home / "settings.json").read_text(encoding="utf-8"))["start_with_windows"] is True
    assert calls and calls[-1][0] is True and "--tray" in calls[-1][1]
    assert main(["autostart", "off"]) == 0
    assert json.loads((isolated_home / "settings.json").read_text(encoding="utf-8"))["start_with_windows"] is False
    assert calls[-1][0] is False


def test_autostart_needs_on_or_off(isolated_home):
    assert main(["autostart"]) == 2
    assert main(["autostart", "maybe"]) == 2


def test_quit_waits_until_the_app_has_ended(isolated_home, monkeypatch):
    app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    requests = []

    def fake_request(msg, timeout=15.0):
        requests.append(msg)
        return {"ok": True, "pid": app.pid}

    monkeypatch.setattr(ipc, "request", fake_request)
    started = time.monotonic()
    assert main(["quit"]) == 0
    assert app.poll() is not None  # it really waited for the process
    assert requests == [{"type": "quit"}]
    assert time.monotonic() - started < 30


def test_quit_when_downpour_is_not_running(isolated_home, monkeypatch):
    def not_running(msg, timeout=15.0):
        raise ipc.NotRunning("Downpour is not running")

    monkeypatch.setattr(ipc, "request", not_running)
    assert main(["quit"]) == 0


def test_wait_for_exit_of_a_process_that_never_existed():
    assert winutil.wait_for_exit(999_999_99, 0.1) is True
