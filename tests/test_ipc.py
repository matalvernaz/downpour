"""The per-user pipe key: created once, reused, and repaired if corrupt."""

from __future__ import annotations

import logging

from downpour.core import ipc


def test_load_key_creates_then_reuses(isolated_home):
    key = ipc.load_key(create=True)
    assert key is not None and len(key) == 32
    assert ipc.load_key(create=True) == key  # a later start reuses the same key


def test_load_key_without_create_is_none_when_absent(isolated_home):
    assert ipc.load_key(create=False) is None


def test_a_corrupt_key_is_replaced_not_left_blocking_startup(isolated_home, caplog):
    (isolated_home / "ipc.key").write_bytes(b"short")  # a kill during the first write leaves a stub
    with caplog.at_level(logging.WARNING):
        key = ipc.load_key(create=True)
    assert key is not None and len(key) == 32  # a fresh, usable key instead of an OSError
    assert (isolated_home / "ipc.key").read_bytes() == key  # written back for next time
