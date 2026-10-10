"""End-to-end engine tests against a real local HTTP server."""

from __future__ import annotations

import hashlib
import os
import sys
import time

import pytest

from downpour.core.models import Download, Status
from tests.helpers import payload, read, wait_for, wait_until


def add(engine, url, **fields):
    return engine.add(Download(url=url, **fields), start=True, force=True)


def test_small_file_single_connection(server, make_engine):
    data = payload(50_000)
    url = server.add("/files/small.bin", data)
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert dl.filename == "small.bin"
    assert read(dl.path) == data
    assert dl.size == len(data) and dl.resumable
    assert not os.path.exists(dl.part_path)
    assert len(server.requests_for("/files/small.bin")) == 1


def test_large_file_uses_many_connections(server, make_engine):
    data = payload(3 * 1024 * 1024)
    url = server.add("/big.bin", data, delay=0.003)
    engine = make_engine(max_connections=8)
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == data
    assert server.peak["/big.bin"] >= 4
    assert len(dl.segments) == 1  # compacted on completion


def test_server_without_ranges(server, make_engine):
    data = payload(400_000)
    url = server.add("/norange.bin", data, ranges=False, delay=0.001)
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == data
    assert dl.resumable is False
    assert server.peak["/norange.bin"] == 1


def test_unknown_length_chunked(server, make_engine):
    data = payload(300_000)
    url = server.add("/stream", data, ranges=False, chunked=True, content_type="application/zip")
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == data
    assert dl.size == len(data)
    assert dl.filename == "stream.zip"


def test_empty_file(server, make_engine):
    url = server.add("/empty.txt", b"")
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == b""


def test_content_disposition_and_redirect(server, make_engine):
    data = payload(10_000)
    target = server.add(
        "/cdn/abc123", data, disposition="attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"
    )
    url = server.add("/download?id=7", redirect_to=target)
    server.files["/download"] = server.files.pop("/download?id=7")
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert dl.filename == "résumé.pdf"
    assert read(dl.path) == data


def test_pause_and_resume_continues_where_it_stopped(server, make_engine):
    data = payload(2 * 1024 * 1024)
    url = server.add("/slow.bin", data, delay=0.01)
    engine = make_engine(max_connections=4)
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > len(data) // 4, message="a quarter downloaded")
    engine.pause([dl.id])
    wait_for(engine, dl.id, Status.PAUSED)
    saved = dl.downloaded
    assert os.path.exists(dl.part_path)
    before = len(server.requests_for("/slow.bin"))
    server.files["/slow.bin"].delay = 0
    engine.start([dl.id])
    wait_for(engine, dl.id, Status.COMPLETED)
    assert read(dl.path) == data
    resumed = server.requests_for("/slow.bin")[before:]
    assert resumed and all((r.start or 0) > 0 for r in resumed)
    assert saved > 0


def test_resume_after_restart(server, make_engine, tmp_path):
    data = payload(2 * 1024 * 1024)
    url = server.add("/restart.bin", data, delay=0.01)
    first = make_engine(max_connections=2)
    dl = add(first, url)
    wait_until(lambda: dl.downloaded > 300_000, message="some progress")
    first.pause([dl.id])
    wait_for(first, dl.id, Status.PAUSED)
    first.shutdown()

    server.files["/restart.bin"].delay = 0
    second = make_engine(max_connections=2)
    again = second.get(dl.id)
    assert again.status == Status.PAUSED and again.downloaded > 0
    before = len(server.requests_for("/restart.bin"))
    second.start([dl.id])
    done = wait_for(second, dl.id, Status.COMPLETED)
    assert read(done.path) == data
    assert all((r.start or 0) > 0 for r in server.requests_for("/restart.bin")[before:])


def test_running_downloads_continue_after_restart(server, make_engine):
    data = payload(1024 * 1024)
    url = server.add("/carry.bin", data, delay=0.01)
    first = make_engine(max_connections=1)
    dl = add(first, url)
    wait_until(lambda: dl.downloaded > 100_000, message="some progress")
    first.shutdown()
    assert dl.status == Status.QUEUED
    server.files["/carry.bin"].delay = 0
    second = make_engine()
    assert second.get(dl.id).status == Status.QUEUED
    second.begin()
    done = wait_for(second, dl.id, Status.COMPLETED)
    assert read(done.path) == data


def test_transient_errors_are_retried(server, make_engine, fast_retry):
    data = payload(200_000)
    url = server.add("/flaky.bin", data, fail_first=3)
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == data


def test_dropped_connection_recovers(server, make_engine, fast_retry):
    data = payload(1024 * 1024)
    url = server.add("/drop.bin", data, drop_after=200_000)
    engine = make_engine(max_connections=1)
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == data


def test_not_found_fails_with_explanation(server, make_engine):
    url = server.add("/gone.bin", status=404)
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.FAILED)
    assert "404" in dl.error and "not on the server" in dl.error


def test_capped_ranges_are_stitched_together(server, make_engine):
    data = payload(1024 * 1024)
    url = server.add("/capped.bin", data, max_range=100_000)
    engine = make_engine(max_connections=3)
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == data


def test_connection_limit_is_respected(server, make_engine, fast_retry):
    data = payload(2 * 1024 * 1024)
    url = server.add("/limited.bin", data, max_conn=2, refuse_status=403, delay=0.002)
    engine = make_engine(max_connections=8)
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert read(dl.path) == data
    assert server.peak["/limited.bin"] <= 2
    assert server.refused >= 1


def test_checksum_verification(server, make_engine):
    data = payload(100_000)
    url = server.add("/sum.bin", data)
    engine = make_engine()
    good = add(engine, url, checksum="sha256:" + hashlib.sha256(data).hexdigest())
    done = wait_for(engine, good.id, Status.COMPLETED)
    assert done.checksum_ok is True

    url2 = server.add("/sum2.bin", data)
    bad = add(engine, url2, checksum="md5:" + "0" * 32)
    failed = wait_for(engine, bad.id, Status.FAILED)
    assert failed.checksum_ok is False and "Checksum mismatch" in failed.error
    assert not os.path.exists(failed.path)


def test_duplicate_names_get_numbered(server, make_engine):
    data = payload(5_000)
    url = server.add("/dup.txt", data)
    engine = make_engine()
    first = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    second = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert first.filename == "dup.txt"
    assert second.filename == "dup (2).txt"


def test_overwrite_setting_replaces_existing(server, make_engine):
    data = payload(5_000)
    url = server.add("/over.txt", data)
    engine = make_engine(duplicate_action="overwrite")
    wait_for(engine, add(engine, url).id, Status.COMPLETED)
    server.files["/over.txt"].data = b"new contents"
    second = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    assert second.filename == "over.txt"
    assert read(second.path) == b"new contents"


def test_file_changed_on_server_restarts(server, make_engine):
    data = payload(1024 * 1024)
    url = server.add("/changes.bin", data, delay=0.01)
    engine = make_engine(max_connections=2)
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 100_000, message="some progress")
    engine.pause([dl.id])
    wait_for(engine, dl.id, Status.PAUSED)
    newer = payload(1024 * 1024 + 7, seed=2)
    server.files["/changes.bin"].data = newer
    server.files["/changes.bin"].delay = 0
    engine.start([dl.id])
    done = wait_for(engine, dl.id, Status.COMPLETED)
    assert read(done.path) == newer


def test_same_size_replacement_on_the_server_starts_over(server, make_engine):
    data = payload(1024 * 1024)
    url = server.add("/nightly.bin", data, delay=0.01, etag='"v1"')
    engine = make_engine(max_connections=2)
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 100_000, message="some progress")
    engine.pause([dl.id])
    wait_for(engine, dl.id, Status.PAUSED)
    newer = payload(len(data), seed=2)
    spec = server.files["/nightly.bin"]
    spec.data, spec.etag, spec.delay = newer, '"v2"', 0
    before = len(server.requests_for("/nightly.bin"))
    engine.start([dl.id])
    done = wait_for(engine, dl.id, Status.COMPLETED)
    assert read(done.path) == newer
    assert server.requests_for("/nightly.bin")[before].headers.get("If-Range") == '"v1"'


def test_unchanged_file_resumes_with_if_range(server, make_engine):
    data = payload(1024 * 1024)
    url = server.add("/stable.bin", data, delay=0.01, etag='"same"')
    engine = make_engine(max_connections=2)
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 100_000, message="some progress")
    engine.pause([dl.id])
    wait_for(engine, dl.id, Status.PAUSED)
    server.files["/stable.bin"].delay = 0
    before = len(server.requests_for("/stable.bin"))
    engine.start([dl.id])
    done = wait_for(engine, dl.id, Status.COMPLETED)
    assert read(done.path) == data
    resumed = server.requests_for("/stable.bin")[before:]
    assert resumed[0].headers.get("If-Range") == '"same"'
    assert all((r.start or 0) > 0 for r in resumed)


def test_pause_does_not_cancel_a_removal(server, make_engine):
    url = server.add("/doomed.bin", payload(1024 * 1024), delay=0.02)
    engine = make_engine(max_connections=1)  # slow enough to still be running
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 0, message="some progress")
    with engine._lock:  # the stopping task cannot finish until both calls are in
        engine.remove([dl.id])
        engine.pause([dl.id])
    # The removal finishes on the task's thread, the file last.
    wait_until(lambda: engine.get(dl.id) is None and not os.path.exists(dl.part_path), message="removal")


def test_pause_after_download_again_starts_over_paused(server, make_engine):
    url = server.add("/redo.bin", payload(1024 * 1024), delay=0.02)
    engine = make_engine(max_connections=1)  # slow enough to still be running
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 0, message="some progress")
    with engine._lock:  # the stopping task cannot finish until both calls are in
        engine.redownload(dl.id)
        engine.pause([dl.id])
    # The reset runs on the task's thread just after it stops.
    wait_until(lambda: dl.downloaded == 0 and not dl.segments, message="the reset")
    assert dl.status == Status.PAUSED and not engine.is_running(dl.id)
    assert not os.path.exists(dl.part_path)


def test_speed_limit(server, make_engine):
    data = payload(300_000)
    url = server.add("/limited-speed.bin", data)
    engine = make_engine(speed_limit=200_000, speed_limit_enabled=True)
    started = time.monotonic()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    elapsed = time.monotonic() - started
    assert read(dl.path) == data
    assert elapsed >= 0.8  # 300 KB at 200 KB/s with a half-second burst allowance


def test_queue_limits_concurrent_downloads(server, make_engine):
    engine = make_engine(max_active=2)
    ids = []
    for i in range(4):
        url = server.add(f"/q{i}.bin", payload(200_000, seed=i), delay=0.005)
        ids.append(engine.add(Download(url=url)).id)
    wait_until(lambda: engine.totals()[0] == 2, message="two running")
    assert engine.totals()[1] == 2
    for i in ids:
        wait_for(engine, i, Status.COMPLETED)


def test_remove_and_redownload(server, make_engine):
    data = payload(100_000)
    url = server.add("/again.bin", data)
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    engine.redownload(dl.id)
    again = wait_for(engine, dl.id, Status.COMPLETED)
    assert again.filename == "again.bin" and read(again.path) == data
    engine.remove([dl.id], delete_files=True)
    wait_until(lambda: engine.get(dl.id) is None, message="removal")
    assert not os.path.exists(dl.path)


def test_redownload_keeps_the_chosen_folder(server, make_engine, tmp_path):
    url = server.add("/kept.bin", payload(10_000))
    engine = make_engine()
    folder = str(tmp_path / "chosen")
    dl = wait_for(engine, add(engine, url, directory=folder).id, Status.COMPLETED)
    engine.redownload(dl.id)
    again = wait_for(engine, dl.id, Status.COMPLETED)
    assert again.directory == folder and os.path.exists(again.path)


def test_refresh_address_keeps_progress(server, make_engine):
    data = payload(1024 * 1024)
    url = server.add("/old-link.bin", data, delay=0.01, etag='"old-server"')
    engine = make_engine(max_connections=2)
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 200_000, message="some progress")
    engine.pause([dl.id])
    wait_for(engine, dl.id, Status.PAUSED)
    server.files["/old-link.bin"].status = 403
    new_url = server.add("/new-link.bin", data, etag='"other-server"')
    engine.refresh_url(dl.id, new_url)
    engine.start([dl.id])
    done = wait_for(engine, dl.id, Status.COMPLETED)
    assert read(done.path) == data
    assert all((r.start or 0) > 0 for r in server.requests_for("/new-link.bin"))


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams")
def test_mark_of_the_web(server, make_engine):
    url = server.add("/motw.exe", payload(1000))
    engine = make_engine()
    dl = wait_for(engine, add(engine, url, referrer="https://example.com/page").id, Status.COMPLETED)
    with open(dl.path + ":Zone.Identifier", encoding="utf-8") as f:
        text = f.read()
    assert "ZoneId=3" in text and "HostUrl=" + url in text and "ReferrerUrl=https://example.com/page" in text


def test_cookies_and_headers_are_sent(server, make_engine):
    url = server.add("/private.bin", payload(1000))
    engine = make_engine()
    cookies = [{"name": "session", "value": "abc", "domain": "127.0.0.1", "path": "/", "hostOnly": True}]
    dl = add(engine, url, cookies=cookies, referrer="https://ref.example/", user_agent="TestAgent/1")
    wait_for(engine, dl.id, Status.COMPLETED)
    req = server.requests_for("/private.bin")[0]
    assert req.headers.get("Cookie") == "session=abc"
    assert req.headers.get("Referer") == "https://ref.example/"
    assert req.headers.get("User-Agent") == "TestAgent/1"


def test_credentials_are_sealed_at_rest(server, make_engine, tmp_path):
    url = server.add("/x.bin", payload(10))
    engine = make_engine()
    dl = engine.add(Download(url=url, password="hunter2", cookies=[{"name": "a", "value": "secretcookie"}]), start=False)
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("test.db*"))
    assert b"hunter2" not in raw and b"secretcookie" not in raw
    engine.shutdown()
    again = make_engine().get(dl.id)
    assert again.password == "hunter2" and again.cookies[0]["value"] == "secretcookie"


def test_rejects_non_http(make_engine):
    engine = make_engine()
    with pytest.raises(ValueError):
        engine.add(Download(url="file:///C:/Windows/win.ini"))


def test_empty_replies_fail_instead_of_looping(server, make_engine, fast_retry):
    data = payload(1024 * 1024)
    url = server.add("/empty-ranges.bin", data, empty_ranges=True, delay=0.002)
    engine = make_engine(max_connections=4, max_retries=3)
    dl = add(engine, url)
    done = wait_for(engine, dl.id, {Status.COMPLETED, Status.FAILED}, timeout=30)
    # The first connection can still finish everything on its own; either way
    # the download must end rather than spin on empty replies.
    if done.status == Status.COMPLETED:
        assert read(done.path) == data


def test_bot_check_refusal_is_explained(server, make_engine):
    url = server.add("/guarded.zip", status=403)
    engine = make_engine()

    from tests import helpers

    original = helpers._Handler._plain

    def challenge(self, status):
        self.send_response(status)
        self.send_header("Server", "cloudflare")
        self.send_header("cf-mitigated", "challenge")
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    helpers._Handler._plain = challenge
    try:
        dl = wait_for(engine, add(engine, url).id, Status.FAILED)
    finally:
        helpers._Handler._plain = original
    assert "bot protection" in dl.error and "browser instead" in dl.error


def test_skip_setting_leaves_existing_file_alone(server, make_engine):
    data = payload(5_000)
    url = server.add("/skip.txt", data)
    engine = make_engine(duplicate_action="skip")
    first = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    server.files["/skip.txt"].data = b"new contents"
    second = wait_for(engine, add(engine, url).id, Status.FAILED)
    assert "already exists" in second.error
    assert read(first.path) == data
    assert not os.path.exists(first.path + ".part")


def test_finished_file_gets_the_server_date(server, make_engine):
    url = server.add("/dated.bin", payload(5_000))
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    if not dl.last_modified:
        pytest.skip("the test server sends no Last-Modified header")
    import email.utils

    expected = email.utils.parsedate_to_datetime(dl.last_modified).timestamp()
    assert abs(os.path.getmtime(dl.path) - expected) < 2


def test_rename_and_move_a_finished_download(server, make_engine, tmp_path):
    url = server.add("/move.txt", payload(3_000))
    engine = make_engine()
    dl = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    old = dl.path
    engine.relocate(dl.id, "renamed.txt", str(tmp_path / "elsewhere"))
    assert dl.filename == "renamed.txt" and dl.directory == str(tmp_path / "elsewhere")
    assert os.path.exists(dl.path) and not os.path.exists(old)
    # A name that is taken gets numbered, like a new download would.
    other = wait_for(engine, add(engine, url).id, Status.COMPLETED)
    engine.relocate(other.id, "renamed.txt", str(tmp_path / "elsewhere"))
    assert other.filename == "renamed (2).txt"


def test_rename_moves_the_partial_file_of_a_paused_download(server, make_engine):
    url = server.add("/slow.bin", payload(600_000), delay=0.05)
    engine = make_engine(max_connections=1)
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 0, message="some bytes")
    with pytest.raises(ValueError):
        engine.relocate(dl.id, "early.bin")  # still running
    engine.pause([dl.id])
    wait_for(engine, dl.id, Status.PAUSED)
    engine.relocate(dl.id, "early.bin")
    assert dl.filename == "early.bin" and os.path.exists(dl.part_path)
    engine.start([dl.id])
    done = wait_for(engine, dl.id, Status.COMPLETED)
    assert done.path.endswith("early.bin") and os.path.getsize(done.path) == 600_000


def test_timed_speed_limit_applies_only_in_its_hours():
    import time as _time

    from downpour.core.settings import Settings

    s = Settings(speed_limit=1000, speed_limit_enabled=True, timed_limit_enabled=True, timed_limit=50,
                 timed_limit_start="08:00", timed_limit_end="23:00")
    day = _time.strptime("2026-10-10 12:00", "%Y-%m-%d %H:%M")
    night = _time.strptime("2026-10-10 02:00", "%Y-%m-%d %H:%M")
    assert s.effective_speed_limit(day) == 50
    assert s.effective_speed_limit(night) == 1000
    s.timed_limit_start, s.timed_limit_end = "22:00", "06:00"  # wraps past midnight
    assert s.effective_speed_limit(night) == 50
    assert s.effective_speed_limit(day) == 1000
    s.timed_limit_enabled = False
    assert s.effective_speed_limit(day) == 1000


def test_a_crawling_download_is_reconnected(server, make_engine, fast_retry):
    data = payload(400_000)
    # The first connection crawls; after the reconnect the server serves at full speed.
    url = server.add("/crawl.bin", data, delay=0.6, max_range=64 * 1024)
    engine = make_engine(max_connections=1, stall_speed=50_000, stall_seconds=3)
    dl = add(engine, url)
    wait_until(lambda: dl.downloaded > 0, timeout=20, message="first bytes")
    server.files["/crawl.bin"].delay = 0.0
    done = wait_for(engine, dl.id, Status.COMPLETED, timeout=60)
    assert read(done.path) == data


def test_rename_before_the_download_has_a_folder(make_engine):
    engine = make_engine()
    dl = engine.add(Download(url="https://example.com/x.zip"), start=False)
    assert dl.directory == ""
    engine.relocate(dl.id, "better.zip")
    assert dl.filename == "better.zip" and dl.filename_fixed and dl.directory == ""
