r"""Count the accessibility WinEvents Downpour raises, by event and window
class. Used to prove live progress does not make NVDA chatter.

    python tools\a11y\hook_events.py SECONDS
"""
import ctypes, ctypes.wintypes as wt, sys, time, collections
user32 = ctypes.windll.user32
oleacc = ctypes.windll.oleacc
seconds = float(sys.argv[1])
hwnd_main = 0
for _ in range(100):
    hwnd_main = user32.FindWindowW(None, "Downpour")
    if hwnd_main:
        break
    time.sleep(0.1)
pid_out = wt.DWORD()
user32.GetWindowThreadProcessId(hwnd_main, ctypes.byref(pid_out))
pid = pid_out.value
print("hooking pid", pid, "hwnd", hwnd_main)
NAMES = {0x800C: "NAMECHANGE", 0x800E: "VALUECHANGE", 0x800A: "STATECHANGE", 0x8004: "REORDER",
         0x8005: "FOCUS", 0x8006: "SELECTION", 0x8000: "CREATE", 0x8001: "DESTROY", 0x800B: "LOCATIONCHANGE",
         0x8002: "SHOW", 0x8003: "HIDE", 0x8007: "SELECTIONADD", 0x8008: "SELECTIONREMOVE", 0x8009: "SELECTIONWITHIN"}
counts = collections.Counter()
samples = collections.defaultdict(list)
WinEventProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
cls = ctypes.create_unicode_buffer(64)
def cb(hook, event, hwnd, obj, child, thread, ts):
    if event == 0x800B:  # location changes are noise
        return
    user32.GetClassNameW(hwnd, cls, 64)
    key = (NAMES.get(event, hex(event)), cls.value, obj, "child" if child else "self")
    counts[key] += 1
proc = WinEventProc(cb)
hook = user32.SetWinEventHook(0x8000, 0x80FF, None, proc, pid, 0, 0)
msg = wt.MSG()
end = time.time() + seconds
while time.time() < end:
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
    time.sleep(0.01)
user32.UnhookWinEvent(hook)
for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(n, key)
