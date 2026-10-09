"""Registering the native host, and the extension agreeing with the host.

Registry tests write only below HKEY_CURRENT_USER\\Software\\DownpourTest and
delete it afterwards; they also check that the real browser keys for the
host are exactly as they were before.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from downpour.browser import install, native_host
from downpour.browser.ids import CHROMIUM_EXTENSION_IDS, FIREFOX_EXTENSION_ID, HOST_NAME, chromium_id_from_key

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "extension"
TEST_PREFIX = r"Software\DownpourTest"
ORIGIN = f"chrome-extension://{CHROMIUM_EXTENSION_IDS[0]}/"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="native hosts are registered in the Windows registry")


def manifest() -> dict:
    return json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))


# -- pure functions --------------------------------------------------------------


def test_chromium_manifest():
    m = install.chromium_manifest(r"C:\Downpour\Downpour.exe")
    assert m == {
        "name": HOST_NAME,
        "description": install.DESCRIPTION,
        "path": r"C:\Downpour\Downpour.exe",
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext}/" for ext in CHROMIUM_EXTENSION_IDS],
    }


def test_firefox_manifest():
    m = install.firefox_manifest(r"C:\Downpour\Downpour.exe")
    assert m["allowed_extensions"] == [FIREFOX_EXTENSION_ID]
    assert "allowed_origins" not in m
    assert m["name"] == HOST_NAME and m["type"] == "stdio"


def test_unknown_family_is_refused():
    with pytest.raises(ValueError):
        install.build_manifest("safari", "x")


def test_host_name_follows_browser_rules():
    assert re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)*", HOST_NAME)


def test_host_key():
    chrome = install.BROWSERS[0]
    assert install.host_key(chrome) == rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
    assert install.host_key(chrome, TEST_PREFIX + "\\") == rf"{TEST_PREFIX}\Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"


def test_firefox_manifest_name_is_recognised_by_the_host(tmp_path):
    path = tmp_path / install.manifest_name(install.FIREFOX)
    assert native_host.is_native_host_invocation(["Downpour.exe", str(path), "someone@else.example"])


def test_launcher_script():
    text = install.launcher_script(r"C:\Py 3\python.exe", r"C:\src\100%\downpour")
    lines = text.split("\r\n")
    assert lines[0] == "@echo off"
    assert 'set "PYTHONPATH=C:\\src\\100%%\\downpour"' in lines
    assert '"C:\\Py 3\\python.exe" -m downpour.browser.native_host %*' in lines
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    assert "chcp" not in text


def test_launcher_script_switches_to_utf8_for_other_alphabets():
    text = install.launcher_script(r"C:\Users\Jürgen\python.exe", r"C:\src")
    assert text.split("\r\n")[1] == "chcp 65001 >nul"


def test_frozen_app_is_its_own_host(monkeypatch, tmp_path):
    exe = tmp_path / "Downpour.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert install.host_program() == exe.resolve()
    files = install.expected_files()
    assert not any(path.suffix == ".cmd" for path in files)
    for text in files.values():
        assert json.loads(text)["path"] == str(exe.resolve())


# -- the extension agrees with the host ------------------------------------------


def test_extension_ids_match():
    m = manifest()
    assert chromium_id_from_key(m["key"]) == CHROMIUM_EXTENSION_IDS[0]
    assert m["browser_specific_settings"]["gecko"]["id"] == FIREFOX_EXTENSION_ID
    background = (EXTENSION / m["background"]["service_worker"]).read_text(encoding="utf-8")
    assert f'"{HOST_NAME}"' in background


def test_manifest_shape():
    m = manifest()
    assert m["manifest_version"] == 3
    assert m["background"]["scripts"] == [m["background"]["service_worker"]]
    assert set(m["permissions"]) == {
        "downloads", "cookies", "contextMenus", "storage", "scripting", "activeTab", "nativeMessaging", "webRequest",
    }
    assert m["host_permissions"] == ["<all_urls>"]
    assert m["browser_specific_settings"]["gecko"]["data_collection_permissions"] == {"required": ["none"]}


def test_manifest_files_exist_and_icons_have_their_sizes():
    m = manifest()
    for name in (m["background"]["service_worker"], m["action"]["default_popup"]):
        assert (EXTENSION / name).is_file(), name
    icons = {**m["icons"], **m["action"]["default_icon"]}
    for size, name in icons.items():
        data = (EXTENSION / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (int(size), int(size)), name


def test_no_private_key_in_the_repository():
    for folder in (EXTENSION, ROOT / "tools", ROOT / "downpour" / "browser"):
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix != ".pyc":
                assert b"PRIVATE KEY" not in path.read_bytes(), path


def test_popup_controls_are_real_labelled_controls():
    class Popup(HTMLParser):
        def __init__(self):
            super().__init__()
            self.checkboxes, self.labels, self.buttons, self.status = set(), set(), [], False
            self._label_for = None
            self._in_button = False

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "input" and a.get("type") == "checkbox":
                self.checkboxes.add(a["id"])
            elif tag == "label":
                self.labels.add(a.get("for"))
            elif tag == "button":
                self._in_button = True
                self.buttons.append("")
            if a.get("role") == "status":
                self.status = True

        def handle_endtag(self, tag):
            if tag == "button":
                self._in_button = False

        def handle_data(self, data):
            if self._in_button:
                self.buttons[-1] += data.strip()

    popup = Popup()
    popup.feed((EXTENSION / "popup.html").read_text(encoding="utf-8"))
    assert popup.checkboxes == {"capture", "captureSite", "capturePrivate"}
    assert popup.checkboxes <= popup.labels
    assert {"Grab video or audio from this page", "Open Downpour"} <= set(popup.buttons)
    assert all(popup.buttons)  # every button has visible text
    assert popup.status


# -- the registry ------------------------------------------------------------------


def _delete_tree(path: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(path + "\\" + child)
    except FileNotFoundError:
        return
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


def _real_registrations() -> dict:
    import winreg

    found = {}
    for browser in install.BROWSERS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, install.host_key(browser)) as key:
                found[browser.id] = winreg.QueryValueEx(key, "")[0]
        except FileNotFoundError:
            found[browser.id] = None
    return found


@pytest.fixture
def test_key():
    import winreg

    before = _real_registrations()
    _delete_tree(TEST_PREFIX)
    yield TEST_PREFIX
    _delete_tree(TEST_PREFIX)
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_PREFIX)
    assert _real_registrations() == before


def _value(path: str) -> str:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        return winreg.QueryValueEx(key, "")[0]


@windows_only
def test_register_status_unregister(test_key, isolated_home):
    assert all(str(s) == "not registered" for s in install.status(key_prefix=test_key).values())

    assert install.register(key_prefix=test_key) == [b.id for b in install.BROWSERS]

    launcher = isolated_home / install.LAUNCHER_NAME
    assert launcher.is_file()
    for browser in install.BROWSERS:
        path = Path(_value(install.host_key(browser, test_key)))
        assert path == isolated_home / install.manifest_name(browser.family)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["path"] == str(launcher)
        assert ("allowed_extensions" in data) == (browser.family == install.FIREFOX)

    report = install.status(key_prefix=test_key)
    assert list(report) == [b.name for b in install.BROWSERS]
    assert all(s.working and s.current and str(s) == "registered" for s in report.values())

    (isolated_home / install.manifest_name(install.FIREFOX)).unlink()
    report = install.status(key_prefix=test_key)
    assert not report["Mozilla Firefox"].manifest_exists and "missing" in str(report["Mozilla Firefox"])
    assert report["Google Chrome"].working

    launcher.write_text("@echo off\r\nrem stale\r\n", encoding="ascii")
    report = install.status(key_prefix=test_key)
    assert not report["Google Chrome"].current and "out of date" in str(report["Google Chrome"])

    install.register(key_prefix=test_key)  # repairs both
    assert all(s.current for s in install.status(key_prefix=test_key).values())

    assert install.unregister(["chrome"], key_prefix=test_key) == ["chrome"]
    report = install.status(key_prefix=test_key)
    assert not report["Google Chrome"].registered and report["Microsoft Edge"].registered
    assert launcher.is_file()  # others still use it

    assert install.unregister(key_prefix=test_key) == ["edge", "chromium", "brave", "firefox"]
    assert not launcher.exists()
    assert not any(isolated_home.glob(f"{HOST_NAME}.*.json"))
    assert install.unregister(key_prefix=test_key) == []


@windows_only
def test_register_some_browsers(test_key, isolated_home):
    with pytest.raises(ValueError):
        install.register(["safari"], key_prefix=test_key)
    assert not (isolated_home / install.LAUNCHER_NAME).exists()  # refused before writing anything

    assert install.register(["firefox"], key_prefix=test_key) == ["firefox"]
    report = install.status(key_prefix=test_key)
    assert [name for name, s in report.items() if s.registered] == ["Mozilla Firefox"]


@windows_only
def test_generated_launcher_runs_the_host(isolated_home):
    """Start the .cmd the way Chrome does and talk to it: nothing but frames
    on stdout, and hello answers without starting the app."""
    install.write_files()
    launcher = isolated_home / install.LAUNCHER_NAME
    command = f'cmd.exe /d /s /c ""{launcher}" {ORIGIN} --parent-window=0"'
    hello = json.dumps({"type": "hello"}).encode()
    proc = subprocess.run(
        command,
        input=struct.pack("<I", len(hello)) + hello,
        capture_output=True,
        cwd=str(isolated_home),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    (length,) = struct.unpack("<I", proc.stdout[:4])
    assert len(proc.stdout) == 4 + length
    reply = json.loads(proc.stdout[4:])
    assert reply["ok"] is False and reply["running"] is False
