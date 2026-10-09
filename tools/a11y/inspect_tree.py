r"""Print what UIA and MSAA expose for Downpour's windows: names, roles,
values - what NVDA sees. Needs pywinauto (pip install pywinauto); run it
while a launcher is up.

    python tools\a11y\inspect_tree.py
"""
import sys, time
from pywinauto import Desktop

def dump(el, depth=0, limit=[0]):
    if limit[0] > 400:
        return
    limit[0] += 1
    info = el.element_info
    try:
        legacy = el.legacy_properties()
    except Exception:
        legacy = {}
    name = info.name
    lname = legacy.get("Name", "")
    role = legacy.get("Role", "")
    value = legacy.get("Value", "")
    print("  " * depth + f"[{info.control_type}] uia={name!r} msaa={lname!r} role={role} value={value!r}")
    if info.control_type in ("ListItem",) and depth > 6:
        return
    for child in el.children():
        dump(child, depth + 1)

deadline = time.time() + 8
wins = []
while time.time() < deadline:
    wins = [w for w in Desktop(backend="uia").windows() if w.window_text() in ("Downpour", "New download")]
    if len(wins) >= 2:
        break
    time.sleep(0.5)
for w in wins:
    print("=" * 20, w.window_text())
    dump(w)
