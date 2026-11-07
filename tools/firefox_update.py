"""Keep updates/firefox.json in step with the signed Firefox add-on.

Firefox asks the address in the add-on's gecko.update_url (this file, as
served from the main branch) for newer versions about once a day, and
installs a newer signed one by itself. Run this after copying a newly signed
add-on to installer/firefox/downpour-firefox.xpi, with the tag the release
is about to get:

    python tools/firefox_update.py v1.2.3

It adds (or replaces) the entry for that add-on's version, linking to the
file as committed under that tag on GitHub, with its SHA-256. Commit both,
then push the tag before main, so the list never names a file that GitHub
cannot serve yet.

Every signed copy keeps the update address for good: never move this file,
make the repository private or rename the main branch, or Firefox users are
stranded on their version until they add a new one by hand.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from downpour.browser.ids import FIREFOX_EXTENSION_ID  # noqa: E402

REPO = "matalvernaz/downpour"
XPI = ROOT / "installer" / "firefox" / "downpour-firefox.xpi"
UPDATES = ROOT / "updates" / "firefox.json"
UPDATE_URL = f"https://raw.githubusercontent.com/{REPO}/main/updates/firefox.json"


def xpi_version(path: Path = XPI) -> str:
    with zipfile.ZipFile(path) as z:
        return str(json.loads(z.read("manifest.json"))["version"])


def sha256(path: Path = XPI) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def link(tag: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{tag}/installer/firefox/downpour-firefox.xpi"


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def load(path: Path | None = None) -> dict:
    path = path or UPDATES
    if not path.exists():
        return {"addons": {FIREFOX_EXTENSION_ID: {"updates": []}}}
    return json.loads(path.read_text(encoding="utf-8"))


def entries(data: dict) -> list[dict]:
    return data["addons"][FIREFOX_EXTENSION_ID]["updates"]


def add(tag: str) -> dict:
    """The update list with an entry for the bundled add-on under ``tag``."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError(f"expected a release tag like v1.2.3, not {tag!r}")
    data = load()
    version = xpi_version()
    updates = [u for u in entries(data) if u.get("version") != version]
    updates.append({"version": version, "update_link": link(tag), "update_hash": "sha256:" + sha256()})
    updates.sort(key=lambda u: version_key(u["version"]))
    data["addons"][FIREFOX_EXTENSION_ID]["updates"] = updates
    return data


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    try:
        data = add(argv[0])
    except ValueError as e:
        print(e)
        return 2
    UPDATES.parent.mkdir(exist_ok=True)
    UPDATES.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    newest = entries(data)[-1]
    print(f"{UPDATES.relative_to(ROOT)}: version {newest['version']} at {newest['update_link']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
