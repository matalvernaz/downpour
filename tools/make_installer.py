"""Build dist\\DownpourSetup-<version>.exe from the app in dist\\Downpour.

    build.cmd
    python tools/make_installer.py

Needs Inno Setup 6 (winget install JRSoftware.InnoSetup). The script is
installer/downpour.iss. The setup bundles installer/firefox/downpour-firefox.xpi,
the Mozilla-signed add-on, when it is there: sign a new one with web-ext and
copy it there whenever extension/manifest.json's version changes.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_icons  # noqa: E402

from downpour import __version__  # noqa: E402

ICON_SIZES = (16, 24, 32, 48, 64, 256)
XPI = ROOT / "installer" / "firefox" / "downpour-firefox.xpi"


def find_iscc() -> str:
    candidates = [
        os.environ.get("ISCC", ""),
        shutil.which("ISCC") or "",
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    sys.exit("Inno Setup 6 was not found: winget install JRSoftware.InnoSetup")


def write_ico(path: Path) -> Path:
    """The raindrop as a Windows icon: PNG images inside an ICO container,
    which every Windows since Vista reads."""
    images = [(size, make_icons.png(size, make_icons.render(size))) for size in ICON_SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        side = 0 if size >= 256 else size  # 0 means 256 in an ICO entry
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + blobs)
    return path


def main() -> int:
    if not (ROOT / "dist" / "Downpour" / "Downpour.exe").is_file():
        sys.exit("dist\\Downpour is missing; run build.cmd first")
    icon = write_ico(ROOT / "build" / "installer" / "downpour.ico")
    args = [find_iscc(), f"/DAppVersion={__version__}", f"/DIconFile={icon}"]
    if XPI.is_file():
        args.append(f"/DFirefoxXpi={XPI}")
    else:
        print(f"No signed Firefox add-on at {XPI}; the setup will not offer Firefox.")
    args.append(str(ROOT / "installer" / "downpour.iss"))
    subprocess.run(args, check=True)
    print(f"Built {ROOT / 'dist' / f'DownpourSetup-{__version__}.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
