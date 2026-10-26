"""Logging: what goes in the files, what never does, and that logging
itself cannot fail the app."""

from __future__ import annotations

import json
import logging

import pytest

from downpour.browser import native_host
from downpour.core import ipc, logs
from downpour.core import settings as settings_mod
from downpour.core.models import Download, Status
from tests.helpers import payload, wait_for, wait_until


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


# -- redaction ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://user:pw@example.com/a.zip#part", "https://example.com/a.zip"),
        ("https://example.com/f?token=abc&name=disc.iso", "https://example.com/f?token=<hidden>&name=disc.iso"),
        (
            "https://b.blob.core.windows.net/x?sp=r&se=2026-10-03T09%3A28%3A06Z&sig=AbC%2Fd",
            "https://b.blob.core.windows.net/x?sp=r&se=2026-10-03T09:28:06Z&sig=<hidden>",
        ),
        (
            "https://s3.example.com/1?X-Amz-Date=20260731T112924Z&X-Amz-Signature=2f0829d6",
            "https://s3.example.com/1?X-Amz-Date=20260731T112924Z&X-Amz-Signature=<hidden>",
        ),
        ("https://example.com/d?at=ABswASavlzm5LBUS22QQt20Cq43G", "https://example.com/d?at=<28 characters>"),
        ("http://[::1]:8080/x?session=1", "http://[::1]:8080/x?session=<hidden>"),
        ("", ""),
    ],
)
def test_redact_url(url, expected):
    assert logs.redact_url(url) == expected


def test_redact_url_keeps_long_values_out():
    redacted = logs.redact_url("https://example.com/?blob=" + "x" * 500)
    assert "xxxx" not in redacted and "<500 characters>" in redacted


def test_cookie_summary_counts_without_values():
    text = logs.summarize_cookies([{"name": "sid", "value": "s3cret", "domain": ".example.com"}])
    assert text == "1 cookie for example.com"
    assert logs.summarize_cookies([]) == "no cookies"


# -- files and levels -------------------------------------------------------------


def test_setup_writes_detailed_lines_to_the_logs_folder(isolated_home, restore_logging):
    path = logs.setup("test.log", detailed=True)
    logging.getLogger("downpour.test").debug("hello %s", "there")
    logs.set_detailed(False)
    logging.getLogger("downpour.test").debug("hidden")
    logging.getLogger("downpour.test").info("shown")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert path == isolated_home / "logs" / "test.log"
    text = path.read_text(encoding="utf-8")
    assert "DEBUG downpour.test: hello there" in text and "INFO downpour.test: shown" in text
    assert "hidden" not in text


def test_rotation_refused_by_windows_loses_nothing(isolated_home, restore_logging, monkeypatch):
    path = logs.setup("busy.log", detailed=True)
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, logs._RotatingFile))
    handler.maxBytes = 200

    def refuse(source, dest):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(handler, "rotate", refuse)
    for n in range(20):
        logging.getLogger("downpour.test").info("line %d %s", n, "x" * 40)
    handler.flush()
    text = path.read_text(encoding="utf-8")
    assert all(f"line {n} " in text for n in range(20))


def test_detailed_setting_is_read_without_loading_everything(isolated_home, monkeypatch):
    monkeypatch.delenv("DOWNPOUR_DEBUG", raising=False)
    assert logs.detailed_setting() is True  # no settings file yet
    (isolated_home / "settings.json").write_text(json.dumps({"detailed_logging": False}), encoding="utf-8")
    assert logs.detailed_setting() is False
    monkeypatch.setenv("DOWNPOUR_DEBUG", "1")
    assert logs.detailed_setting() is True


def test_previous_crash_report_is_kept_and_mentioned(isolated_home, monkeypatch, caplog):
    monkeypatch.setattr(logs.faulthandler, "enable", lambda *a, **k: None)
    monkeypatch.setattr(logs, "_crash_file", None)
    crash = isolated_home / "logs" / logs.CRASH_LOG
    crash.parent.mkdir(parents=True, exist_ok=True)
    crash.write_text("Windows fatal exception: access violation\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        logs.enable_crash_log()
    try:
        assert (crash.parent / (logs.CRASH_LOG + ".1")).read_text(encoding="utf-8").startswith("Windows fatal")
        assert "earlier run of Downpour crashed" in caplog.text
    finally:
        if logs._crash_file is not None:
            logs._crash_file.close()


def test_a_fault_report_after_a_clean_exit_is_not_called_a_crash(isolated_home, monkeypatch, caplog):
    monkeypatch.setattr(logs.faulthandler, "enable", lambda *a, **k: None)
    monkeypatch.setattr(logs, "_crash_file", None)
    logs_folder = isolated_home / "logs"
    logs_folder.mkdir(parents=True, exist_ok=True)
    (logs_folder / logs.CRASH_LOG).write_text("Windows fatal exception: code 0x80010108\n", encoding="utf-8")
    logs.mark_clean_exit()  # the previous run reached its shutdown
    with caplog.at_level(logging.DEBUG):
        logs.enable_crash_log()
    try:
        assert (logs_folder / (logs.CRASH_LOG + ".1")).exists()  # the report is still kept
        assert "crashed" not in caplog.text  # but the user is not told a crash happened
        assert not (logs_folder / logs.CLEAN_EXIT).exists()  # the marker is consumed, so the next start can warn
    finally:
        if logs._crash_file is not None:
            logs._crash_file.close()


def test_watchdog_reports_a_frozen_window_with_every_stack(caplog):
    dog = logs.Watchdog(lambda fn: None, timeout=0.2, interval=0.05)  # the "window" never runs anything
    with caplog.at_level(logging.WARNING):
        dog.start()
        try:
            wait_until(lambda: "has not responded" in caplog.text, timeout=5)
        finally:
            dog.stop()
    assert "--- MainThread" in caplog.text


# -- settings ---------------------------------------------------------------------


def test_unreadable_settings_are_logged_not_silently_reset(isolated_home, caplog):
    (isolated_home / "settings.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        settings = settings_mod.load()
    assert settings == settings_mod.Settings()
    assert "using the defaults" in caplog.text


def test_settings_differences_hide_the_proxy_address():
    old = settings_mod.Settings()
    new = settings_mod.Settings(proxy_url="http://me:pw@proxy:3128", max_active=5)
    new.capture_extensions = old.capture_extensions + ["xyz"]
    changes = settings_mod.differences(old, new)
    assert "max_active: 3 -> 5" in changes
    assert "proxy_url: <empty> -> <set>" in changes
    assert any(c.startswith("capture_extensions: added ['xyz']") for c in changes)
    assert not any("pw@" in c for c in changes)


# -- the extension's log ----------------------------------------------------------


def test_extension_lines_skip_junk_and_flatten_text():
    lines = logs.extension_lines(
        [
            {"t": "2026-10-10T07:42:01.123Z", "level": "warn", "msg": "first\nsecond"},
            {"msg": ""},
            "not an entry",
            {"t": "nonsense", "level": "shout", "msg": "x" * 5000},
        ],
        "extension 0.1.0 Chrome 141",
    )
    assert len(lines) == 2
    assert lines[0].endswith("[extension 0.1.0 Chrome 141] WARNING first | second")
    assert " INFO " in lines[1] and len(lines[1]) < logs.MAX_ENTRY + 100


def test_log_messages_are_written_without_starting_the_app(isolated_home, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a log message must not reach the app")

    monkeypatch.setattr(ipc, "request_or_launch", never)
    monkeypatch.setattr(ipc, "request", never)
    reply = native_host.handle({
        "type": "log", "version": "0.1.0", "browser": "Chrome 141",
        "entries": [{"t": "2026-10-10T07:42:01.000Z", "level": "info", "msg": "download 7: offering it"}],
    })
    assert reply == {"ok": True, "written": 1}
    text = (isolated_home / "logs" / logs.EXTENSION_LOG).read_text(encoding="utf-8")
    assert "[extension 0.1.0 Chrome 141] INFO download 7: offering it" in text


# -- downloads --------------------------------------------------------------------


def test_download_log_tells_the_story_without_secrets(server, make_engine, caplog):
    data = payload(300_000)
    url = server.add("/files/report.bin", data)
    engine = make_engine()
    secret = "cookie-value-that-must-not-leak"
    dl = Download(
        url=url + "?token=signed-secret&name=report",
        username="matt", password="hunter2-password",
        cookies=[{"name": "sid", "value": secret, "domain": "127.0.0.1", "path": "/"}],
        headers={"X-Api-Key": "header-secret"},
    )
    with caplog.at_level(logging.DEBUG):
        engine.add(dl, start=True, force=True)
        wait_for(engine, dl.id, Status.COMPLETED)
        wait_until(lambda: "now completed" in caplog.text)
    text = caplog.text
    assert f"download {dl.id}: starting" in text
    assert f"download {dl.id}: completed" in text
    assert "HTTP 206" in text or "HTTP 200" in text
    assert "token=<hidden>" in text
    for leaked in (secret, "signed-secret", "hunter2-password", "header-secret"):
        assert leaked not in text


def test_failed_download_logs_why(server, make_engine, caplog):
    url = server.add("/gone.bin", b"", status=404)
    engine = make_engine()
    with caplog.at_level(logging.DEBUG):
        dl = engine.add(Download(url=url), start=True, force=True)
        wait_for(engine, dl.id, Status.FAILED)
        wait_until(lambda: "now failed" in caplog.text)
    assert "HTTP 404" in caplog.text
    assert f"download {dl.id}: failed after" in caplog.text


def test_queue_finishing_is_logged(server, make_engine, caplog):
    url = server.add("/one.bin", payload(10_000))
    engine = make_engine()
    with caplog.at_level(logging.INFO):
        engine.begin()
        dl = engine.add(Download(url=url), start=True)
        wait_for(engine, dl.id, Status.COMPLETED)
        wait_until(lambda: "nothing left to download" in caplog.text)
    assert f"added download {dl.id}" in caplog.text
