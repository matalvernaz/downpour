"""Updating Downpour from its GitHub releases.

Each release carries DownpourSetup-<version>.exe and a .sha256 file beside
it (see .github/workflows/release.yml). Checking asks GitHub's API for the
recent releases and picks the highest version; updating downloads the
setup, checks it against the published SHA-256, and starts it with
/SILENT /RELAUNCH. The setup closes Downpour the way Exit does (downloads
resume afterwards), replaces the files, and starts it again.

Only a built copy updates itself; running from source, ``supported()`` is
False. Prereleases count: every release so far is one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from downpour import __version__
from downpour.core.paths import data_dir

log = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/matalvernaz/downpour/releases?per_page=20"
SETUP_NAME = re.compile(r"^DownpourSetup-(\d+(?:\.\d+)*)\.exe$", re.I)
CHECK_INTERVAL = 24 * 60 * 60  # seconds between automatic checks


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    notes: str
    page_url: str
    setup_url: str
    setup_name: str
    setup_size: int
    sha256_url: str


def version_key(text: str) -> tuple[int, ...]:
    """"v0.10.2" -> (0, 10, 2); anything after the numbers is ignored."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def is_newer(version: str, than: str = __version__) -> bool:
    return version_key(version) > version_key(than)


def supported() -> bool:
    """A built copy, which has a setup to update it with."""
    return bool(getattr(sys, "frozen", False))


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"Downpour/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_releases(data: object) -> Release | None:
    """The highest-versioned release that has a setup and its checksum."""
    best: Release | None = None
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        assets = {a.get("name", ""): a for a in item.get("assets") or [] if isinstance(a, dict)}
        for name, asset in assets.items():
            match = SETUP_NAME.match(name)
            checksum = assets.get(name + ".sha256")
            if not match or not checksum:
                continue
            release = Release(
                version=match.group(1),
                notes=str(item.get("body") or "").strip(),
                page_url=str(item.get("html_url") or ""),
                setup_url=str(asset.get("browser_download_url") or ""),
                setup_name=name,
                setup_size=int(asset.get("size") or 0),
                sha256_url=str(checksum.get("browser_download_url") or ""),
            )
            if release.setup_url and (best is None or version_key(release.version) > version_key(best.version)):
                best = release
    return best


def latest(url: str = RELEASES_URL, timeout: float = 20.0) -> Release | None:
    """The newest published release, or None when there is none yet."""
    log.info("checking for updates at %s (this is %s)", url.split("?")[0], __version__)
    release = parse_releases(json.loads(_get(url, timeout)))
    if release is None:
        log.info("no release with a setup was found")
    else:
        log.info("newest release: %s (%s bytes), %s", release.version, release.setup_size,
                 "newer" if is_newer(release.version) else "not newer")
    return release


def download(release: Release, progress: Callable[[int, int | None], None] | None = None,
             stop: threading.Event | None = None, folder: Path | None = None) -> Path:
    """Fetch the release's setup and check its SHA-256. Returns its path."""
    folder = folder or data_dir() / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    expected = _get(release.sha256_url, 30).decode("ascii", "replace").split()
    if not expected or not re.fullmatch(r"[0-9a-fA-F]{64}", expected[0]):
        raise UpdateError("The release's checksum file could not be read.")
    target = folder / release.setup_name
    partial = target.with_suffix(".part")
    digest = hashlib.sha256()
    log.info("downloading %s", release.setup_url)
    req = urllib.request.Request(release.setup_url, headers={"User-Agent": f"Downpour/{__version__}"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(partial, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0) or release.setup_size or None
        done = 0
        while chunk := resp.read(256 * 1024):
            if stop is not None and stop.is_set():
                raise InterruptedError("cancelled")
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    actual = digest.hexdigest()
    if actual.lower() != expected[0].lower():
        partial.unlink(missing_ok=True)
        log.error("%s has SHA-256 %s but the release says %s", release.setup_name, actual, expected[0])
        raise UpdateError("The downloaded update did not match its published checksum, so it was deleted.")
    partial.replace(target)
    log.info("downloaded %s (%d bytes), checksum verified", target, done)
    return target


def install(setup: Path) -> None:
    """Start the setup, which closes this copy and starts the new one."""
    args = [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH"]
    log.info("starting the update: %s", " ".join(args))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clean_downloads(folder: Path | None = None) -> None:
    """Remove setups left from earlier updates."""
    folder = folder or data_dir() / "updates"
    if not folder.is_dir():
        return
    for path in folder.glob("DownpourSetup-*"):
        try:
            path.unlink()
            log.debug("removed old update file %s", path)
        except OSError:
            pass
