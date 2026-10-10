"""Pure helpers that live in the GUI package (skipped where wx is missing)."""

from __future__ import annotations

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
