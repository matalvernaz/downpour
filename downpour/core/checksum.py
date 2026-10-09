"""File checksums: computing them, and parsing the ones users paste in."""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Callable

ALGORITHMS = ("md5", "sha1", "sha256", "sha512")
_BY_LENGTH = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def parse(text: str) -> tuple[str, str] | None:
    """Read an expected checksum as ``(algorithm, lowercase hex)``.

    Accepts ``sha256:abc...``, ``SHA-256 abc...`` or a bare hex digest whose
    length identifies the algorithm. Returns None if it is not a checksum.
    """
    text = text.strip()
    if not text:
        return None
    algo = ""
    m = re.match(r"^(md5|sha-?1|sha-?256|sha-?512)\s*[:=\s]\s*(\S+)$", text, re.I)
    if m:
        algo = m.group(1).lower().replace("-", "")
        text = m.group(2)
    if not _HEX.match(text):
        return None
    guessed = _BY_LENGTH.get(len(text))
    if guessed is None or (algo and algo != guessed):
        return None
    return guessed, text.lower()


def compute(
    path: str,
    algorithm: str = "sha256",
    progress: Callable[[int], None] | None = None,
    stop: threading.Event | None = None,
) -> str:
    digest = hashlib.new(algorithm)
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            if stop is not None and stop.is_set():
                raise InterruptedError("checksum cancelled")
            digest.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done)
    return digest.hexdigest()


def compute_all(path: str, stop: threading.Event | None = None) -> dict[str, str]:
    """Every supported digest of ``path`` in a single read."""
    digests = {name: hashlib.new(name) for name in ALGORITHMS}
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            if stop is not None and stop.is_set():
                raise InterruptedError("checksum cancelled")
            for d in digests.values():
                d.update(chunk)
    return {name: d.hexdigest() for name, d in digests.items()}
