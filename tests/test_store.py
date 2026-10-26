"""The download store: credentials round-trip, and survive a transient unseal failure."""

from __future__ import annotations

import json
import logging

from downpour.core import store as store_mod
from downpour.core import winutil
from downpour.core.models import Download


def _boom(_text):
    raise OSError("DPAPI briefly unavailable")


def test_credentials_round_trip():
    dl = Download(url="http://x/f.zip", cookies=[{"name": "s", "value": "v", "domain": "x"}], password="pw")
    loaded = store_mod.Store._decode(1, store_mod.Store._encode(dl))
    assert loaded.cookies == dl.cookies and loaded.password == "pw"


def test_a_failed_unseal_keeps_the_blob_sealed_not_erased(monkeypatch, caplog):
    dl = Download(url="http://x/f.zip", cookies=[{"name": "s", "value": "v", "domain": "x"}])
    text = store_mod.Store._encode(dl)
    assert json.loads(text).get("sealed")  # credentials were sealed at rest

    monkeypatch.setattr(winutil, "unseal", _boom)  # the master key is briefly unavailable
    with caplog.at_level(logging.WARNING):
        loaded = store_mod.Store._decode(1, text)
    assert loaded.cookies == []  # unreadable right now
    assert "keeping them sealed" in caplog.text

    text2 = store_mod.Store._encode(loaded)  # a later save, e.g. a status change
    assert json.loads(text2).get("sealed")  # the blob is preserved, not wiped
