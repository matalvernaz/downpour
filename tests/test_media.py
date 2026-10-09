"""yt-dlp integration, driven through the engine against the local server."""

from __future__ import annotations

import os

import pytest

from downpour.core import media
from downpour.core.models import Download, Status
from tests.helpers import payload, read, wait_for

pytestmark = pytest.mark.skipif(media.find_ytdlp() is None, reason="yt-dlp is not installed")


def test_fetch_info_for_a_direct_video(server, make_engine):
    url = server.add("/clips/holiday.mp4", payload(200_000), content_type="video/mp4")
    engine = make_engine()
    info = media.fetch_info(url, engine.settings)
    assert info.title == "holiday"
    assert not info.is_playlist


def test_media_download_through_the_engine(server, make_engine, tmp_path):
    data = payload(300_000)
    url = server.add("/clips/beach.mp4", data, content_type="video/mp4")
    engine = make_engine()
    dl = Download(url=url, kind="media", media_format="best-mp4", filename="beach.mp4",
                  directory=str(tmp_path / "videos"))
    engine.add(dl, force=True)
    done = wait_for(engine, dl.id, Status.COMPLETED, timeout=60)
    assert done.filename == "beach.mp4"
    assert read(done.path) == data
    assert done.size == len(data)


def test_media_failure_is_reported(server, make_engine, tmp_path):
    url = server.add("/clips/missing.mp4", status=404)
    engine = make_engine()
    dl = Download(url=url, kind="media", media_format="best", filename="x.mp4", directory=str(tmp_path))
    engine.add(dl, force=True)
    failed = wait_for(engine, dl.id, Status.FAILED, timeout=60)
    assert "404" in failed.error


def test_cookie_file_format(isolated_home):
    path = media._cookie_file([
        {"name": "sid", "value": "abc", "domain": "example.com", "path": "/", "secure": True,
         "hostOnly": False, "expirationDate": 2000000000},
        {"name": "h", "value": "1", "domain": "only.example.com", "hostOnly": True, "httpOnly": True},
    ])
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    finally:
        os.remove(path)
    assert lines[1] == ".example.com\tTRUE\t/\tTRUE\t2000000000\tsid\tabc"
    assert lines[2] == "#HttpOnly_only.example.com\tFALSE\t/\tFALSE\t0\th\t1"
