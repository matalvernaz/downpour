"""Updates from GitHub releases: choosing the release, and downloading its
setup only when the published checksum matches."""

from __future__ import annotations

import hashlib
import json

import pytest

from downpour.core import updates
from tests.helpers import payload


def asset(name: str, url: str, size: int = 0) -> dict:
    return {"name": name, "browser_download_url": url, "size": size}


def release(version: str, *, draft: bool = False, prerelease: bool = True, checksum: bool = True) -> dict:
    name = f"DownpourSetup-{version}.exe"
    assets = [asset(name, f"https://example.com/{name}", 100), asset("Downpour-windows.zip", "https://example.com/z")]
    if checksum:
        assets.append(asset(name + ".sha256", f"https://example.com/{name}.sha256"))
    return {"tag_name": f"v{version}", "draft": draft, "prerelease": prerelease, "body": f"Notes for {version}",
            "html_url": f"https://example.com/v{version}", "assets": assets}


@pytest.mark.parametrize("text, key", [("v0.10.2", (0, 10, 2)), ("1.2", (1, 2)), ("0.3.0-beta", (0, 3, 0)), ("x", ())])
def test_version_key(text, key):
    assert updates.version_key(text) == key


def test_newer_compares_numbers_not_text():
    assert updates.is_newer("0.10.0", than="0.9.9")
    assert not updates.is_newer("0.1.0", than="0.1.0")
    assert not updates.is_newer("0.0.9", than="0.1.0")


def test_highest_release_wins_and_drafts_and_unchecked_setups_do_not():
    data = [release("0.2.0"), release("0.10.0", checksum=False), release("0.9.0", draft=True), release("0.3.1")]
    best = updates.parse_releases(data)
    assert best is not None and best.version == "0.3.1"
    assert best.notes == "Notes for 0.3.1" and best.sha256_url.endswith(".exe.sha256")


def test_no_releases_means_no_update():
    assert updates.parse_releases([]) is None
    assert updates.parse_releases({"message": "Not Found"}) is None


def test_latest_reads_the_releases_list(server):
    url = server.add("/releases", json.dumps([release("0.4.0")]).encode())
    found = updates.latest(url)
    assert found is not None and found.version == "0.4.0"


def test_download_checks_the_published_checksum(server, tmp_path):
    data = payload(300_000)
    setup = server.add("/DownpourSetup-0.4.0.exe", data)
    sums = server.add("/DownpourSetup-0.4.0.exe.sha256", f"{hashlib.sha256(data).hexdigest()}  DownpourSetup-0.4.0.exe".encode())
    rel = updates.Release("0.4.0", "", "", setup, "DownpourSetup-0.4.0.exe", len(data), sums)
    seen = []
    path = updates.download(rel, progress=lambda done, total: seen.append((done, total)), folder=tmp_path)
    assert path.read_bytes() == data
    assert seen and seen[-1] == (len(data), len(data))


def test_download_with_the_wrong_checksum_keeps_nothing(server, tmp_path):
    setup = server.add("/DownpourSetup-0.4.0.exe", payload(50_000))
    sums = server.add("/DownpourSetup-0.4.0.exe.sha256", ("0" * 64 + "  DownpourSetup-0.4.0.exe").encode())
    rel = updates.Release("0.4.0", "", "", setup, "DownpourSetup-0.4.0.exe", 50_000, sums)
    folder = tmp_path / "updates"
    with pytest.raises(updates.UpdateError):
        updates.download(rel, folder=folder)
    assert list(folder.iterdir()) == []


def test_old_setups_are_cleaned_up(tmp_path):
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / "DownpourSetup-0.1.0.exe").write_bytes(b"old")
    (folder / "keep.txt").write_text("mine")
    updates.clean_downloads(folder)
    assert [p.name for p in folder.iterdir()] == ["keep.txt"]
