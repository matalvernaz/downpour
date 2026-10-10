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
