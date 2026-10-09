"""Human-readable sizes, speeds and durations.

Durations use words ("2 minutes 5 seconds") rather than clock notation,
because a screen reader reads "2:05" as "2 colon 05".
"""

from __future__ import annotations

import re

_UNITS = ("bytes", "KB", "MB", "GB", "TB")
_SIZE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kmgt]?)(?:i?b)?(?:/s|ps)?\s*$", re.I)


def format_size(n: int | float | None) -> str:
    if n is None:
        return "unknown size"
    n = float(n)
    if n < 1024:
        return f"{int(n)} bytes" if n != 1 else "1 byte"
    for unit in _UNITS[1:]:
        n /= 1024
        if n < 1024 or unit == _UNITS[-1]:
            return f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
    raise AssertionError("unreachable")


def format_speed(bps: float | None) -> str:
    if not bps or bps < 1:
        return ""
    return format_size(bps) + "/s"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    if s < 60:
        return _plural(s, "second")
    m, s = divmod(s, 60)
    if m < 60:
        return _plural(m, "minute") + (f" {_plural(s, 'second')}" if s and m < 10 else "")
    h, m = divmod(m, 60)
    if h < 24:
        return _plural(h, "hour") + (f" {_plural(m, 'minute')}" if m else "")
    d, h = divmod(h, 24)
    return _plural(d, "day") + (f" {_plural(h, 'hour')}" if h else "")


def parse_size(text: str) -> int | None:
    """Parse "500K", "1.5 MB", "2g", "300 KB/s" or "1024" into bytes."""
    m = _SIZE.match(text)
    if not m:
        return None
    factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[m.group(2).lower()]
    return int(float(m.group(1)) * factor)
