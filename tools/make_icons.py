"""Draw the browser extension's icons: a blue raindrop holding a white arrow.

Plain zlib and struct instead of an imaging library, so the extension's only
binary files can be rebuilt from this script on any Python, and the output is
byte-for-byte the same every time. Each pixel is sampled on a 4 x 4 grid for
smooth edges.

Usage: python tools/make_icons.py [output folder]   (default: extension/icons)
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 32, 48, 128)
DROP = (25, 103, 210)
ARROW = (255, 255, 255)
SAMPLES = 4

# Shape, in a unit square with y pointing down.
TIP = (0.5, 0.04)
CENTRE = (0.5, 0.62)
RADIUS = 0.34
_DISTANCE = CENTRE[1] - TIP[1]
_HALF_ANGLE = math.asin(RADIUS / _DISTANCE)
# Below this line the circle itself covers the drop's tapering sides.
_TANGENT_Y = TIP[1] + math.sqrt(_DISTANCE**2 - RADIUS**2) * math.cos(_HALF_ANGLE)


def in_drop(x: float, y: float) -> bool:
    if (x - CENTRE[0]) ** 2 + (y - CENTRE[1]) ** 2 <= RADIUS**2:
        return True
    if not TIP[1] <= y <= _TANGENT_Y:
        return False
    return abs(x - TIP[0]) <= (y - TIP[1]) * math.tan(_HALF_ANGLE)


def in_arrow(x: float, y: float) -> bool:
    if abs(x - 0.5) <= 0.06 and 0.40 <= y <= 0.66:  # shaft
        return True
    if 0.60 <= y <= 0.85:  # head, narrowing to a point at the bottom
        return abs(x - 0.5) <= 0.19 * (0.85 - y) / 0.25
    return False


def render(size: int) -> bytes:
    """RGBA rows, each led by PNG filter byte 0."""
    rows = bytearray()
    step = 1.0 / (size * SAMPLES)
    for py in range(size):
        rows.append(0)
        for px in range(size):
            arrow = drop = 0
            for sy in range(SAMPLES):
                y = (py * SAMPLES + sy + 0.5) * step
                for sx in range(SAMPLES):
                    x = (px * SAMPLES + sx + 0.5) * step
                    if in_drop(x, y):
                        if in_arrow(x, y):
                            arrow += 1
                        else:
                            drop += 1
            covered = arrow + drop
            if not covered:
                rows += b"\0\0\0\0"
                continue
            colour = [round((arrow * a + drop * d) / covered) for a, d in zip(ARROW, DROP)]
            rows += bytes(colour) + bytes([round(255 * covered / SAMPLES**2)])
    return bytes(rows)


def png(size: int, rows: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    out = Path(argv[1]) if len(argv) > 1 else root / "extension" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = out / f"icon-{size}.png"
        path.write_bytes(png(size, render(size)))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
