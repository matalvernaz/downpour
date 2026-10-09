"""Small Windows-specific helpers, each degrading to a no-op elsewhere."""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"
FSCTL_SET_SPARSE = 0x000900C4


def set_sparse(fileobj, sparse: bool = True) -> bool:
    """Mark an open file sparse, or clear the flag again.

    Writing far beyond the end of a normal NTFS file makes Windows zero-fill
    the whole gap first, so a connection starting at the 5 GB mark of a new
    file would stall while 5 GB of zeros are written. Sparse files skip that.
    """
    if not WINDOWS:
        return False
    import msvcrt
    from ctypes import wintypes

    handle = msvcrt.get_osfhandle(fileobj.fileno())
    flag = ctypes.c_ubyte(1 if sparse else 0)
    returned = wintypes.DWORD()
    kernel32 = ctypes.windll.kernel32
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    ok = kernel32.DeviceIoControl(
        handle, FSCTL_SET_SPARSE, ctypes.byref(flag), 1, None, 0, ctypes.byref(returned), None
    )
    return bool(ok)


def write_zone_identifier(path: str, url: str, referrer: str = "") -> None:
    """Add the Mark of the Web, exactly as browsers do.

    SmartScreen, Office Protected View and the "this file came from another
    computer" prompt all depend on it. A download manager that skips it
    quietly lowers the user's defences.
    """
    if not WINDOWS:
        return
    lines = ["[ZoneTransfer]", "ZoneId=3"]
    if referrer:
        lines.append(f"ReferrerUrl={referrer}")
    if url:
        lines.append(f"HostUrl={url}")
    try:
        with open(path + ":Zone.Identifier", "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        log.debug("could not write Zone.Identifier for %s", path, exc_info=True)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob),
    ]
    fn.restype = ctypes.c_int
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def seal(data: bytes) -> str:
    """Encrypt for the current Windows user (DPAPI); base64 text out."""
    if WINDOWS:
        return "dpapi:" + base64.b64encode(_dpapi(data, True)).decode("ascii")
    return "plain:" + base64.b64encode(data).decode("ascii")


def unseal(text: str) -> bytes:
    scheme, _, payload = text.partition(":")
    raw = base64.b64decode(payload)
    if scheme == "dpapi":
        return _dpapi(raw, False)
    return raw


def open_path(path: str) -> None:
    if WINDOWS:
        os.startfile(path)  # noqa: S606 - opening the user's own file is the point
    else:
        subprocess.Popen(["xdg-open", path])


def reveal(path: str) -> None:
    """Open Explorer with ``path`` selected."""
    if WINDOWS and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif os.path.isdir(os.path.dirname(path)):
        open_path(os.path.dirname(path))


def scan_with_defender(path: str) -> tuple[bool, str]:
    """Scan one file with Microsoft Defender; (clean, message)."""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Windows Defender", "MpCmdRun.exe"),
    ]
    platform_dir = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows Defender", "Platform")
    if os.path.isdir(platform_dir):
        for version in sorted(os.listdir(platform_dir), reverse=True):
            candidates.insert(0, os.path.join(platform_dir, version, "MpCmdRun.exe"))
    exe = next((c for c in candidates if os.path.exists(c)), None)
    if exe is None:
        return True, "Microsoft Defender is not available"
    proc = subprocess.run(
        [exe, "-Scan", "-ScanType", "3", "-File", path, "-DisableRemediation"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode == 0:
        return True, "No threats found"
    if proc.returncode == 2:
        return False, "Microsoft Defender found a threat in this file"
    return True, f"Scan did not finish (code {proc.returncode})"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_start_with_windows(enabled: bool, command: str) -> None:
    if not WINDOWS:
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "Downpour", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "Downpour")
            except FileNotFoundError:
                pass


def power_action(action: str) -> None:
    """Sleep, hibernate or shut down the computer."""
    if not WINDOWS:
        return
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "0"])
    elif action in ("sleep", "hibernate"):
        powrprof = ctypes.windll.powrprof
        powrprof.SetSuspendState(action == "hibernate", False, False)
