"""Where Downpour keeps its own files.

Everything lives under one data directory: ``%LOCALAPPDATA%\\Downpour`` by
default. Setting ``DOWNPOUR_HOME`` overrides it, which the tests use to stay
out of the real profile, and a ``portable.txt`` file beside a frozen
executable switches to a ``data`` folder next to it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Folder holding the executable (frozen) or the source tree (dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def resource_dir() -> Path:
    """The ``downpour`` package folder, holding sounds and vendor files.

    PyInstaller unpacks bundled data under ``sys._MEIPASS``; the build puts
    those files under a ``downpour`` folder there to keep this one rule.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "downpour"
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("DOWNPOUR_HOME")
    if override:
        path = Path(override)
    elif getattr(sys, "frozen", False) and (app_dir() / "portable.txt").exists():
        path = app_dir() / "data"
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "Downpour"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def downloads_dir() -> Path:
    """The user's real Downloads folder, even when it has been relocated."""
    if sys.platform == "win32":
        try:
            return Path(_known_folder("{374DE290-123F-4565-9164-39C4925E467B}"))
        except OSError:
            pass
    return Path.home() / "Downloads"


def _known_folder(guid: str) -> str:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder_id = GUID()
    if ctypes.oledll.ole32.CLSIDFromString(guid, ctypes.byref(folder_id)) != 0:
        raise OSError("bad known-folder GUID")
    out = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)
    ]
    if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) != 0:
        raise OSError("SHGetKnownFolderPath failed")
    try:
        return out.value or ""
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)
