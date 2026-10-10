"""Build what the Chrome Web Store needs, in dist\\store:

    downpour-chrome-<version>.zip   the extension, with the Firefox-only and
                                    unpacked-only manifest keys removed
    icon-128.png                    the store icon: 96 px artwork, 16 px margin
    screenshot-1280x800.png         rendered from store/screenshot.html
    promo-440x280.png               rendered from store/promo.html

The images are drawn by headless Chrome, so Chrome must be installed.

    python tools/make_store_assets.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import make_icons  # noqa: E402

EXTENSION = ROOT / "extension"
STORE = ROOT / "store"
OUT = ROOT / "dist" / "store"

# The store assigns its own key, so it rejects "key"; Chrome ignores the
# Firefox keys but warns about them in the store's checks.
DROP_KEYS = ("key", "browser_specific_settings")
CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def store_manifest() -> dict:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    for key in DROP_KEYS:
        manifest.pop(key, None)
    background = manifest.get("background", {})
    background.pop("scripts", None)  # Firefox's event page; Chrome runs the service worker
    return manifest


def build_zip(version: str) -> Path:
    path = OUT / f"downpour-chrome-{version}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(EXTENSION.rglob("*")):
            name = file.relative_to(EXTENSION).as_posix()
            if file.is_dir() or any(part.startswith(".") for part in name.split("/")):
                continue  # web-ext leaves .amo-upload-uuid behind after signing
            if name == "manifest.json":
                archive.writestr(name, json.dumps(store_manifest(), indent=2) + "\n")
            else:
                archive.write(file, name)
    return path


def store_icon() -> Path:
    """The drop drawn at 96 px in the middle of a transparent 128 px square,
    as the store's image guidelines ask."""
    size, art = 128, 96
    margin = (size - art) // 2
    inner = make_icons.render(art)
    stride = 1 + art * 4
    empty_row = b"\0" + b"\0" * (size * 4)
    rows = bytearray()
    for y in range(size):
        if margin <= y < margin + art:
            line = inner[(y - margin) * stride + 1:(y - margin + 1) * stride]
            rows += b"\0" + b"\0" * (margin * 4) + line + b"\0" * (margin * 4)
        else:
            rows += empty_row
    path = OUT / "icon-128.png"
    path.write_bytes(make_icons.png(size, bytes(rows)))
    return path


def render(chrome: str, page: Path, out: Path, width: int, height: int) -> Path:
    with tempfile.TemporaryDirectory(prefix="dp-headless-") as profile:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
             f"--user-data-dir={profile}", f"--window-size={width},{height}", f"--screenshot={out}",
             page.resolve().as_uri()],
            check=True, capture_output=True, timeout=120,
        )
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    version = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))["version"]
    made = [build_zip(version), store_icon()]
    chrome = next((p for p in CHROME_PATHS if os.path.isfile(p)), None)
    if chrome is None:
        print("Chrome was not found, so the screenshot and promo tile were not drawn.", file=sys.stderr)
    else:
        made.append(render(chrome, STORE / "screenshot.html", OUT / "screenshot-1280x800.png", 1280, 800))
        made.append(render(chrome, STORE / "promo.html", OUT / "promo-440x280.png", 440, 280))
    for path in made:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
