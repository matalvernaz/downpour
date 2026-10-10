"""SQLite persistence for download records.

Each record is one JSON document. Credentials (cookies, password, custom
headers) are split out and sealed with DPAPI, so a copied database file is
useless on another account or machine.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from downpour.core import winutil
from downpour.core.models import SECRET_FIELDS, Download

log = logging.getLogger(__name__)


class Store:
    def __init__(self, path: Path | str):
        self._lock = threading.Lock()
        log.info("opening the download database %s (SQLite %s)", path, sqlite3.sqlite_version)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS downloads ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " data TEXT NOT NULL)"
            )

    @staticmethod
    def _encode(dl: Download, segments: list | None = None) -> str:
        data = dl.to_dict()
        if segments is not None:
            data["segments"] = segments
        secrets = {name: data.pop(name) for name in SECRET_FIELDS}
        if any(secrets.values()):
            data["sealed"] = winutil.seal(json.dumps(secrets).encode("utf-8"))
        return json.dumps(data)

    @staticmethod
    def _decode(row_id: int, text: str) -> Download:
        data = json.loads(text)
        sealed = data.pop("sealed", None)
        if sealed:
            try:
                data.update(json.loads(winutil.unseal(sealed)))
            except (OSError, ValueError):
                log.warning("could not unseal credentials for download %s", row_id)
        data["id"] = row_id
        return Download.from_dict(data)

    def load_all(self) -> list[Download]:
        with self._lock:
            rows = self._db.execute("SELECT id, data FROM downloads").fetchall()
        out = []
        for row_id, text in rows:
            try:
                out.append(self._decode(row_id, text))
            except (ValueError, TypeError):
                log.exception("skipping unreadable download record %s", row_id)
        return out

    def insert(self, dl: Download) -> int:
        text = self._encode(dl)
        with self._lock:
            cur = self._db.execute("INSERT INTO downloads (data) VALUES (?)", (text,))
            dl.id = int(cur.lastrowid)
        return dl.id

    def update(self, dl: Download, segments: list | None = None) -> None:
        """Save ``dl``. ``segments`` overrides the live segment list, for
        callers that must save a snapshot taken before an fsync."""
        text = self._encode(dl, segments)
        with self._lock:
            self._db.execute("UPDATE downloads SET data = ? WHERE id = ?", (text, dl.id))

    def delete(self, ids: list[int]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM downloads WHERE id = ?", [(i,) for i in ids])

    def close(self) -> None:
        with self._lock:
            self._db.close()
        log.debug("download database closed")
