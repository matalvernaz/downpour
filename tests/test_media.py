"""yt-dlp integration, driven through the engine against the local server."""

from __future__ import annotations

import os
import shutil
import subprocess

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


def test_same_titles_never_share_a_file(server, make_engine, tmp_path):
    # yt-dlp reports success without downloading when the file exists, so the
    # second download used to "finish" as the first one's file. The name also
    # holds "%(", which yt-dlp would read as a template field.
    first, second = payload(100_000, seed=1), payload(120_000, seed=2)
    urls = [server.add("/a/clip.mp4", first, content_type="video/mp4"),
            server.add("/b/clip.mp4", second, content_type="video/mp4")]
    engine = make_engine()
    folder = str(tmp_path / "videos")
    ids = [engine.add(Download(url=u, kind="media", media_format="single", filename="50%(x)s off.mp4",
                               directory=folder), force=True).id for u in urls]
    done = [wait_for(engine, i, Status.COMPLETED, timeout=60) for i in ids]
    assert [d.filename for d in done] == ["50%(x)s off.mp4", "50%(x)s off (2).mp4"]
    assert read(done[0].path) == first and read(done[1].path) == second


def test_a_download_without_a_name_is_named_after_the_title(server, make_engine, tmp_path):
    url = server.add("/clips/watch.mp4", payload(50_000), content_type="video/mp4")
    engine = make_engine()
    dl = engine.add(Download(url=url, kind="media", media_format="single", directory=str(tmp_path)), force=True)
    assert dl.filename == ""  # not "watch.mp4" guessed from the address
    done = wait_for(engine, dl.id, Status.COMPLETED, timeout=60)
    assert done.filename == "watch.mp4" and os.path.exists(done.path)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_converting_shows_as_finishing(server, make_engine, tmp_path):
    tone = tmp_path / "tone.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1",
                    "-c:a", "aac", str(tone)], check=True, timeout=60)
    url = server.add("/clips/tone.m4a", tone.read_bytes(), content_type="audio/mp4")
    engine = make_engine()
    seen = []
    engine.subscribe(lambda event, d: seen.append(d.status) if d is not None else None)
    dl = engine.add(Download(url=url, kind="media", media_format="mp3", filename="tone.mp3",
                             directory=str(tmp_path / "music")), force=True)
    done = wait_for(engine, dl.id, Status.COMPLETED, timeout=60)
    assert done.filename == "tone.mp3"
    assert Status.FINISHING in seen


def _probe(path: str, stream: str, entry: str = "codec_name") -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", f"stream={entry}",
         "-of", "default=nokey=1:noprint_wrappers=1", path],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    return out.splitlines()[0] if out else ""


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="FFmpeg not installed")
def test_audio_bitrate_reencodes_only_the_audio(server, make_engine, tmp_path):
    # A combined file (media_format "single", no merge) proves the re-encode
    # runs as its own pass, not as a side effect of yt-dlp merging streams.
    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=128x72:rate=10",
         "-f", "lavfi", "-i", "sine=duration=1",
         "-c:v", "mpeg4", "-c:a", "aac", "-b:a", "128k", str(src)],
        check=True, timeout=60,
    )
    url = server.add("/clips/clip.mp4", src.read_bytes(), content_type="video/mp4")
    engine = make_engine()
    dl = Download(url=url, kind="media", media_format="single", filename="clip.mp4",
                  directory=str(tmp_path / "out"), media_options={"audio_bitrate": 32})
    engine.add(dl, force=True)
    done = wait_for(engine, dl.id, Status.COMPLETED, timeout=60)
    assert _probe(done.path, "v") == "mpeg4"  # the video stream is untouched
    source_abr = int(_probe(str(src), "a", "bit_rate") or 0)
    result_abr = int(_probe(done.path, "a", "bit_rate") or 0)
    assert result_abr and result_abr < 80_000  # dropped well below the 128 kbps source
    assert source_abr == 0 or result_abr < source_abr


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_reencoding_audio_keeps_the_file_date(make_engine, tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=128x72:rate=10",
         "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "mpeg4", "-c:a", "aac", str(clip)],
        check=True, timeout=60,
    )
    old = 1_000_000_000  # a fixed time well in the past
    os.utime(clip, (old, old))
    task = media.MediaTask(make_engine(), Download(url="x", kind="media", media_format="single"))
    task._reencode_audio(str(clip), 32, shutil.which("ffmpeg"))
    assert abs(os.path.getmtime(clip) - old) < 2  # the date survived os.replace


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
