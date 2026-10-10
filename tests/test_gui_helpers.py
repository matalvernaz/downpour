"""Pure helpers that live in the GUI package (skipped where wx is missing)."""

from __future__ import annotations

import os

import pytest

dialogs = pytest.importorskip("downpour.gui.dialogs")


def test_links_keep_their_own_brackets():
    text = "See https://en.wikipedia.org/wiki/Python_(programming_language). Or (https://e.com/a.zip)"
    assert dialogs.urls_in(text) == [
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "https://e.com/a.zip",
    ]


def test_sentence_punctuation_is_not_part_of_a_link():
    assert dialogs.urls_in("Get it at https://e.com/f.iso, then https://e.com/f.iso.") == ["https://e.com/f.iso"]


def test_server_date_parsing_and_setting(tmp_path):
    from downpour.core import winutil

    path = tmp_path / "dated.txt"
    path.write_text("x")
    assert winutil.set_file_time(str(path), "Sat, 03 Oct 2026 07:09:38 GMT")
    assert int(os.path.getmtime(path)) == 1791011378
    assert not winutil.set_file_time(str(path), "not a date")
    assert not winutil.set_file_time(str(path), "")


def test_recycle_refuses_missing_files(tmp_path):
    from downpour.core import winutil

    assert winutil.recycle(str(tmp_path / "nothing.txt")) is False


def test_sorting_and_searching_the_list():
    from downpour.core.models import Download, Status
    from downpour.gui.download_list import matches, sort_rows

    a = Download(url="https://x/a.zip", filename="alpha.zip", size=5, added_at=2.0, status=Status.COMPLETED)
    b = Download(url="https://x/b.zip", filename="Beta.zip", size=50, added_at=1.0, status=Status.DOWNLOADING)
    c = Download(url="https://x/c.zip", filename="gamma.zip", size=None, added_at=3.0, status=Status.PAUSED, description="Book one")
    rows = [a, b, c]
    assert sort_rows(rows, "queue", False) == rows
    assert sort_rows(rows, "name", False) == [a, b, c]
    assert sort_rows(rows, "name", True) == [c, b, a]
    assert sort_rows(rows, "size", False) == [c, a, b]
    assert sort_rows(rows, "added", False) == [b, a, c]
    assert sort_rows(rows, "status", False) == [b, c, a]
    assert [d for d in rows if matches(d, ["zip"])] == rows
    assert [d for d in rows if matches(d, ["book", "gamma"])] == [c]
    assert [d for d in rows if matches(d, ["nothing"])] == []


def test_categories_match_sites_before_file_types():
    from downpour.core.settings import Category, Settings

    s = Settings(categories=[Category("Books", ["epub"], sites=["archive.org"]), Category("Music", ["mp3"])])
    assert s.category_for("x.mp3", "https://dl.archive.org/a/x.mp3").name == "Books"
    assert s.category_for("x.mp3", "https://example.com/x.mp3").name == "Music"
    assert s.category_for("x.mp3").name == "Music"
    assert s.category_for("x.txt", "https://example.com/x.txt") is None
    assert str(s.folder_for("x.mp3", "https://archive.org/x.mp3")).endswith("Books")


def test_recent_folders_dedupe_and_stay_short():
    from downpour.core import settings as settings_mod

    s = settings_mod.Settings()
    for i in range(12):
        settings_mod.remember_folder(s, rf"C:\f{i}")
    settings_mod.remember_folder(s, r"c:\F11")  # same as the newest, other case
    settings_mod.remember_folder(s, "")
    assert len(s.recent_folders) == settings_mod.RECENT_FOLDERS
    assert s.recent_folders[0] == r"c:\F11" and r"C:\f11" not in s.recent_folders
