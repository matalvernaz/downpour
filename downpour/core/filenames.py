"""Turning URLs and server headers into safe Windows file names."""

from __future__ import annotations

import email.message
import email.utils
import mimetypes
import os
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows device names. Windows 10 also reserves them followed by any
# extension ("nul.txt", "con.tar.gz"), so the part before the first dot counts.
_RESERVED = (
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{n}" for port in ("COM", "LPT") for n in [*"0123456789", "¹", "²", "³"]}
)
# Extensions that name the script serving a file rather than the file itself.
_SCRIPT_EXTENSIONS = {".php", ".asp", ".aspx", ".jsp", ".cgi", ".pl", ".py", ".do", ".action"}
MAX_NAME = 200


def sanitize(name: str, fallback: str = "download") -> str:
    """Make ``name`` a legal single path component on Windows."""
    name = _INVALID.sub("_", name).strip().rstrip(". ")
    name = name.lstrip(".") or name
    if not name:
        return fallback
    stem, ext = os.path.splitext(name)
    if stem.split(".")[0].rstrip(" ").upper() in _RESERVED:
        stem = "_" + stem
    if len(stem) + len(ext) > MAX_NAME:
        ext = ext[:20]
        stem = stem[: MAX_NAME - len(ext)].rstrip(". ")
    return stem + ext


def from_content_disposition(value: str | None) -> str | None:
    """The file name a Content-Disposition header asks for, if any.

    ``filename*`` (RFC 5987) wins over plain ``filename`` as RFC 6266 says.
    Servers that put raw UTF-8 in ``filename`` arrive as Latin-1 mojibake,
    because http.client decodes header bytes as Latin-1; that is undone here.
    """
    if not value:
        return None
    msg = email.message.Message()
    msg["content-disposition"] = value
    plain = encoded = None
    for key, val in (msg.get_params(header="content-disposition") or [])[1:]:
        if key.lower() != "filename":
            continue
        if isinstance(val, tuple):
            encoded = email.utils.collapse_rfc2231_value(val)
        else:
            plain = val
    name = encoded or plain
    if not name:
        return None
    if not encoded:
        try:
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    # Some servers send a path; only the last component is ours to use.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    return name.strip() or None


def from_url(url: str) -> str:
    parts = urlsplit(url)
    name = unquote(posixpath.basename(parts.path))
    return name or parts.hostname or "download"


def guess_extension(mime: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if not mime or mime in ("application/octet-stream", "binary/octet-stream"):
        return ""
    ext = mimetypes.guess_extension(mime, strict=False) or ""
    return ext


def choose(url: str, disposition: str | None, mime: str) -> str:
    """Pick the name for a download from everything the server told us."""
    name = from_content_disposition(disposition)
    if not name:
        name = from_url(url)
        stem, ext = os.path.splitext(name)
        guessed = guess_extension(mime)
        if guessed and (not ext or ext.lower() in _SCRIPT_EXTENSIONS):
            name = (stem if ext.lower() in _SCRIPT_EXTENSIONS else name) + guessed
    return sanitize(name)


def unique_path(path: Path, taken: set[str] | None = None) -> Path:
    """``path``, or ``name (2).ext`` and so on when it is already in use.

    A name counts as used if the file exists, its ``.part`` exists, or another
    download has claimed it (``taken`` holds lower-cased full paths).
    """
    taken = taken or set()

    def in_use(p: Path) -> bool:
        return p.exists() or Path(str(p) + ".part").exists() or str(p).lower() in taken

    if not in_use(path):
        return path
    stem, ext = path.stem, path.suffix
    for i in range(2, 10000):
        candidate = path.with_name(f"{stem} ({i}){ext}")
        if not in_use(candidate):
            return candidate
    raise OSError(f"No free file name for {path}")


def unique_stem(folder: Path, name: str, taken: set[str] | None = None) -> str:
    """``name``, or ``name (2).ext`` and so on, so that nothing in ``folder``
    starts with the same stem, whatever its extension.

    For yt-dlp downloads, whose real extension is only known at the end.
    yt-dlp treats an existing file of the same name as already downloaded and
    reports success without fetching anything. ``taken`` holds lower-cased
    stems that other downloads have claimed.
    """
    taken = taken or set()
    stem, ext = os.path.splitext(name)
    try:
        existing = [entry.lower() for entry in os.listdir(folder)]
    except OSError:
        existing = []

    def in_use(candidate: str) -> bool:
        key = candidate.lower()
        return key in taken or any(entry.startswith(key + ".") for entry in existing)

    if not in_use(stem):
        return name
    for i in range(2, 10000):
        candidate = f"{stem} ({i})"
        if not in_use(candidate):
            return candidate + ext
    raise OSError(f"No free file name for {name} in {folder}")
