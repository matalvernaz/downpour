"""Registering the native-messaging host with the browsers.

A browser finds a native host through a registry key named after the host,
whose default value is the path of a small JSON manifest. The manifest names
the program to start and the extensions allowed to start it. Chromium-family
browsers and Firefox want different fields in it (allowed_origins versus
allowed_extensions), so two manifests are written into the data directory and
each browser's key points at the right one.

Everything lives under HKEY_CURRENT_USER: no administrator rights are needed,
other accounts are unaffected, and unregistering leaves nothing behind.

When frozen, the program in the manifests is Downpour's own executable, which
notices the browser's calling convention at startup and becomes the host.
From source there is no such executable and a browser cannot pass arguments,
so a generated .cmd file in the data directory runs the host module instead.

Manifest and launcher text come from pure functions, kept apart from the
file and registry writes so they can be tested anywhere.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from downpour.browser.ids import CHROMIUM_EXTENSION_IDS, FIREFOX_EXTENSION_ID, HOST_NAME
from downpour.core.paths import app_dir, data_dir

DESCRIPTION = "Downpour download manager"
LAUNCHER_NAME = "native-host.cmd"
CHROMIUM = "chromium"
FIREFOX = "firefox"


@dataclass(frozen=True)
class Browser:
    id: str
    name: str
    registry_path: str  # under HKEY_CURRENT_USER; the host's own key goes beneath it
    family: str  # CHROMIUM or FIREFOX: which manifest the browser reads


# Chromium builds fall back to Chrome's key and Edge falls back to both, but
# each gets its own entry so that unregistering one browser leaves the others
# working. Other Chromium browsers that read Chrome's key (Vivaldi, for one)
# are covered by the Chrome entry.
BROWSERS: tuple[Browser, ...] = (
    Browser("chrome", "Google Chrome", r"Software\Google\Chrome\NativeMessagingHosts", CHROMIUM),
    Browser("edge", "Microsoft Edge", r"Software\Microsoft\Edge\NativeMessagingHosts", CHROMIUM),
    Browser("chromium", "Chromium", r"Software\Chromium\NativeMessagingHosts", CHROMIUM),
    Browser("brave", "Brave", r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts", CHROMIUM),
    Browser("firefox", "Mozilla Firefox", r"Software\Mozilla\NativeMessagingHosts", FIREFOX),
)


@dataclass(frozen=True)
class BrowserStatus:
    browser: Browser
    registered: bool  # the browser's registry key for the host exists
    manifest: str = ""  # the manifest path that key points at
    manifest_exists: bool = False
    program: str = ""  # the program that manifest starts
    program_exists: bool = False
    current: bool = False  # exactly what register() would write now

    @property
    def working(self) -> bool:
        return self.registered and self.manifest_exists and self.program_exists

    def __str__(self) -> str:
        """One line for the Options dialog, e.g. "registered"."""
        if not self.registered:
            return "not registered"
        if not self.manifest_exists:
            return "registered, but its manifest file is missing; install the browser helper again"
        if not self.program_exists:
            return "registered, but the helper program is missing; install the browser helper again"
        if not self.current:
            return "registered, but out of date; install the browser helper again"
        return "registered"


# ---------------------------------------------------------------------------
# Pure functions


def chromium_manifest(program: str, extension_ids: Iterable[str] = CHROMIUM_EXTENSION_IDS) -> dict:
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": program,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext}/" for ext in extension_ids],
    }


def firefox_manifest(program: str, extension_id: str = FIREFOX_EXTENSION_ID) -> dict:
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": program,
        "type": "stdio",
        "allowed_extensions": [extension_id],
    }


def build_manifest(family: str, program: str) -> dict:
    if family == CHROMIUM:
        return chromium_manifest(program)
    if family == FIREFOX:
        return firefox_manifest(program)
    raise ValueError(f"unknown browser family: {family!r}")


def manifest_text(family: str, program: str) -> str:
    return json.dumps(build_manifest(family, program), indent=2) + "\n"


def manifest_name(family: str) -> str:
    """File name of a host manifest; the host recognises Firefox's call by it."""
    return f"{HOST_NAME}.{family}.json"


def host_key(browser: Browser, prefix: str = "") -> str:
    """Registry path, under HKEY_CURRENT_USER, of the browser's key for the host."""
    parts = (prefix.strip("\\"), browser.registry_path, HOST_NAME)
    return "\\".join(part for part in parts if part)


def launcher_script(python: str, source_root: str) -> str:
    """The .cmd a browser runs to start the host from a source checkout.

    Percent signs are doubled because cmd.exe expands them even inside
    quotes. cmd.exe reads a batch file in the console code page, so a script
    holding non-ASCII paths first switches that page to UTF-8; its output
    goes to nul because standard output is the browser's message channel.
    """

    def literal(text: str) -> str:
        return text.replace("%", "%%")

    lines = [
        "@echo off",
        "rem Starts Downpour's browser helper from a source checkout.",
        "rem Written by downpour.browser.install; registering again rewrites it.",
        "setlocal DisableDelayedExpansion",
        f'set "PYTHONPATH={literal(source_root)}"',
        f'"{literal(python)}" -m downpour.browser.native_host %*',
    ]
    if not all(line.isascii() for line in lines):
        lines.insert(1, "chcp 65001 >nul")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Files


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def host_program() -> Path:
    """What the browsers start: the frozen app itself, or the source launcher.

    A frozen build ships a windowed Downpour.exe and a console
    downpour-cli.exe; the host must be the console one, because only a
    console program is sure to get the browser's stdin and stdout.
    """
    if is_frozen():
        cli = app_dir() / "downpour-cli.exe"
        return cli if cli.exists() else Path(sys.executable).resolve()
    return data_dir() / LAUNCHER_NAME


def _console_python() -> str:
    """The console interpreter; the GUI may be running under pythonw.exe."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def expected_files() -> dict[Path, str]:
    """Every file register() writes, with its exact contents."""
    program = host_program()
    files: dict[Path, str] = {}
    if not is_frozen():
        files[program] = launcher_script(_console_python(), str(app_dir()))
    for family in (CHROMIUM, FIREFOX):
        files[data_dir() / manifest_name(family)] = manifest_text(family, str(program))
    return files


def write_files() -> None:
    """Write the manifests (and launcher), each atomically so a browser
    starting a host at that moment never reads half a file."""
    for path, text in expected_files().items():
        temp = path.with_name(path.name + ".tmp")
        temp.write_bytes(text.encode("utf-8"))
        os.replace(temp, path)


def _remove_files() -> None:
    for path in expected_files():
        path.unlink(missing_ok=True)


def _matches(path: Path, text: str) -> bool:
    try:
        return path.read_bytes() == text.encode("utf-8")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Registry


def _select(browsers: Iterable[str] | None) -> list[Browser]:
    if browsers is None:
        return list(BROWSERS)
    by_id = {browser.id: browser for browser in BROWSERS}
    chosen = []
    for name in browsers:
        if name not in by_id:
            raise ValueError(f"unknown browser: {name!r}")
        chosen.append(by_id[name])
    return chosen


def register(browsers: Iterable[str] | None = None, *, key_prefix: str = "") -> list[str]:
    """Write the host files and point each browser at them.

    ``browsers`` holds ids from BROWSERS (all of them by default); the ids
    registered are returned. ``key_prefix`` is prepended to every registry
    path so tests can stay inside a throwaway key.
    """
    import winreg

    chosen = _select(browsers)
    write_files()
    for browser in chosen:
        manifest = data_dir() / manifest_name(browser.family)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, host_key(browser, key_prefix), 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
    return [browser.id for browser in chosen]


def unregister(browsers: Iterable[str] | None = None, *, key_prefix: str = "") -> list[str]:
    """Remove the browsers' registry keys; returns the ids that were registered.

    Once no browser is left pointing at the host, its files are deleted too.
    """
    import winreg

    removed = []
    for browser in _select(browsers):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, host_key(browser, key_prefix))
        except FileNotFoundError:
            continue
        removed.append(browser.id)
    if not any(state.registered for state in status(key_prefix=key_prefix).values()):
        _remove_files()
    return removed


def status(*, key_prefix: str = "") -> dict[str, BrowserStatus]:
    """Registration state of every browser in BROWSERS.

    Keyed by the browser's display name, so ``f"{name}: {state}"`` reads as
    a sentence; ``state.browser.id`` is the id register() takes.
    """
    import winreg

    expected = expected_files()
    result: dict[str, BrowserStatus] = {}
    for browser in BROWSERS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, host_key(browser, key_prefix)) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, "")
                except FileNotFoundError:
                    value = ""
        except FileNotFoundError:
            result[browser.name] = BrowserStatus(browser, registered=False)
            continue
        manifest = Path(str(value)) if value else None
        program = ""
        if manifest is not None:
            try:
                program = str(json.loads(manifest.read_text(encoding="utf-8")).get("path", ""))
            except (OSError, ValueError, AttributeError):
                program = ""
        program_path = Path(program) if program else None
        if program_path is not None and manifest is not None and not program_path.is_absolute():
            program_path = manifest.parent / program_path  # Windows hosts may be relative
        wanted = data_dir() / manifest_name(browser.family)
        current = (
            manifest is not None
            and os.path.normcase(str(manifest)) == os.path.normcase(str(wanted))
            and _matches(wanted, expected[wanted])
            and (is_frozen() or _matches(host_program(), expected[host_program()]))
        )
        result[browser.name] = BrowserStatus(
            browser,
            registered=True,
            manifest=str(manifest or ""),
            manifest_exists=bool(manifest and manifest.is_file()),
            program=str(program_path or ""),
            program_exists=bool(program_path and program_path.is_file()),
            current=current,
        )
    return result
