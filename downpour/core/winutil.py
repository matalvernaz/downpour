"""Small Windows-specific helpers, each degrading to a no-op elsewhere."""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import subprocess
import sys
import time

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


def parent_process() -> str:
    """Name and ID of the process that started this one, such as
    "chrome.exe (1234)": which browser called the helper, for its log."""
    if not WINDOWS:
        return f"process {os.getppid()}"
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    parent = os.getppid()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return f"process {parent}"
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == parent:
                return f"{entry.szExeFile} ({parent})"
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return f"process {parent}"


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
_awake = False


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping while downloads run (the display may still
    turn off). The flag belongs to the calling thread, so the same thread
    must set and clear it; the window's timer does both."""
    global _awake
    if not WINDOWS or on == _awake:
        return
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    if ctypes.windll.kernel32.SetThreadExecutionState(flags):
        _awake = on
        log.info("keeping the computer awake" if on else "the computer may sleep again")


FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


def recycle(path: str) -> bool:
    """Send a file to the Recycle Bin. False when Windows refused (a
    network or removable drive, or a file that is in use)."""
    if not WINDOWS or not os.path.exists(path):
        return False
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"  # double NUL ends the list
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    result = shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        log.info("the Recycle Bin refused %s (code %s)", path, result)
        return False
    return True


def set_file_time(path: str, last_modified: str) -> bool:
    """Give a file the date the server gave it (an HTTP Last-Modified
    header). False when the header could not be read."""
    import email.utils

    try:
        when = email.utils.parsedate_to_datetime(last_modified)
    except (TypeError, ValueError, IndexError):
        return False
    if when is None:
        return False
    try:
        stamp = when.timestamp()
        os.utime(path, (stamp, stamp))
    except (OSError, OverflowError, ValueError):
        log.debug("could not set the date of %s", path, exc_info=True)
        return False
    return True


def run_after_download(command: str, path: str) -> None:
    """Start the user's command for a finished file, [File] replaced by the
    file's path. It runs detached; failures are logged, never shown."""
    filled = command.replace("[File]", f'"{path}"') if "[File]" in command else f'{command} "{path}"'
    try:
        subprocess.Popen(filled, shell=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("ran after download: %s", filled)
    except OSError as e:
        log.warning("could not run the after-download command %r: %s", command, e)


def open_path(path: str) -> None:
    log.debug("opening %s", path)
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


SCAN_TIMEOUT = 15 * 60  # seconds; a big disc image can take minutes


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
        log.info("Defender scan skipped: MpCmdRun.exe was not found")
        return True, "Microsoft Defender is not available"
    began = time.monotonic()
    try:
        proc = subprocess.run(
            [exe, "-Scan", "-ScanType", "3", "-File", path, "-DisableRemediation"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("Defender scan of %s stopped after %d seconds", path, SCAN_TIMEOUT)
        return True, "The scan took too long and was stopped"
    log.info("Defender scanned %s in %.1f s: exit code %s", path, time.monotonic() - began, proc.returncode)
    log.debug("Defender said: %s", " | ".join((proc.stdout or "").strip().splitlines()[-8:]))
    if proc.returncode == 0:
        return True, "No threats found"
    if proc.returncode == 2:
        return False, "Microsoft Defender found a threat in this file"
    return True, f"Scan did not finish (code {proc.returncode})"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    """What the Run key should start: the window app, in the notification area.

    Built from the program folder rather than sys.executable, because the
    command line (downpour-cli.exe) sets this too, and it must start the
    window app beside it.
    """
    from downpour.core.paths import app_dir

    if getattr(sys, "frozen", False):
        return f'"{app_dir() / "Downpour.exe"}" --tray'
    exe = sys.executable
    windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
    runner = windowless if os.path.exists(windowless) else exe
    code = f"import sys; sys.path.insert(0, r'{app_dir()}'); from downpour.__main__ import main; sys.exit(main(['downpour', '--tray']))"
    return f'"{runner}" -c "{code}"'


def wait_for_exit(pid: int, timeout: float) -> bool:
    """True once process ``pid`` has ended (or never existed); False if it
    is still running after ``timeout`` seconds."""
    if not WINDOWS:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.2)
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0  # WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def set_start_with_windows(enabled: bool, command: str) -> None:
    if not WINDOWS:
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "Downpour", 0, winreg.REG_SZ, command)
            log.debug("start with Windows: %s", command)
        else:
            try:
                winreg.DeleteValue(key, "Downpour")
                log.info("start with Windows turned off")
            except FileNotFoundError:
                pass


def start_with_windows_command() -> str:
    """What the Run key starts at sign-in, or "" when nothing is set."""
    if not WINDOWS:
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, "Downpour")[0])
    except OSError:
        return ""


def power_action(action: str) -> None:
    """Sleep, hibernate or shut down the computer."""
    if not WINDOWS:
        return
    log.info("all downloads finished; power action: %s", action)
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "0"])
    elif action in ("sleep", "hibernate"):
        powrprof = ctypes.windll.powrprof
        powrprof.SetSuspendState(action == "hibernate", False, False)
