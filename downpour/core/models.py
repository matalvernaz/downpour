"""The download record: everything Downpour remembers about one download."""

from __future__ import annotations

import enum
import os
import time
from dataclasses import dataclass, field, fields

from downpour.core.segments import Segment


class Status(str, enum.Enum):
    QUEUED = "queued"  # waiting for a free slot; starts automatically
    CONNECTING = "connecting"
    DOWNLOADING = "downloading"
    FINISHING = "finishing"  # checksum, rename, post-processing
    PAUSED = "paused"  # stopped by the user; never starts on its own
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Status.QUEUED: "Queued",
    Status.CONNECTING: "Connecting",
    Status.DOWNLOADING: "Downloading",
    Status.FINISHING: "Finishing",
    Status.PAUSED: "Paused",
    Status.COMPLETED: "Complete",
    Status.FAILED: "Failed",
}

ACTIVE = frozenset({Status.CONNECTING, Status.DOWNLOADING, Status.FINISHING})
UNFINISHED = frozenset({Status.QUEUED, Status.PAUSED, Status.FAILED}) | ACTIVE

# Fields that hold credentials. The store encrypts them at rest.
SECRET_FIELDS = ("cookies", "password", "headers")
# Live figures recomputed while running; never persisted.
TRANSIENT_FIELDS = ("speed", "eta", "active_connections")


@dataclass
class Download:
    url: str
    id: int = 0
    kind: str = "http"  # http | media
    filename: str = ""
    filename_fixed: bool = False  # the user chose the name; ignore server hints
    directory: str = ""
    referrer: str = ""
    page_url: str = ""
    description: str = ""
    size: int | None = None
    downloaded: int = 0
    status: Status = Status.QUEUED
    error: str = ""
    resumable: bool | None = None
    segments: list[Segment] = field(default_factory=list)
    connections: int = 0  # 0 = use the global setting
    speed_limit: int = 0  # bytes per second, 0 = unlimited
    user_agent: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[dict] = field(default_factory=list)
    username: str = ""
    password: str = ""
    checksum: str = ""  # expected digest, "algorithm:hex"
    checksum_ok: bool | None = None
    mime: str = ""
    etag: str = ""
    last_modified: str = ""
    media_format: str = ""
    # Extra choices for video and audio (see core/media.py MEDIA_OPTIONS):
    # audio_language, subtitles, sponsorblock, metadata, clip_start, clip_end.
    media_options: dict = field(default_factory=dict)
    added_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    position: float = 0.0

    speed: float = 0.0
    eta: float | None = None
    active_connections: int = 0

    @property
    def path(self) -> str:
        if not self.filename:
            return ""
        return os.path.join(self.directory, self.filename)

    @property
    def part_path(self) -> str:
        return self.path + ".part" if self.path else ""

    @property
    def progress(self) -> float | None:
        """Fraction complete, or None when the size is unknown."""
        if self.status == Status.COMPLETED:
            return 1.0
        if not self.size:
            return None
        return min(1.0, self.downloaded / self.size)

    def display_name(self) -> str:
        return self.filename or self.url

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            if f.name in TRANSIENT_FIELDS:
                continue
            value = getattr(self, f.name)
            if f.name == "segments":
                value = [s.to_list() for s in value]
            elif f.name == "status":
                value = value.value
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Download":
        known = {f.name for f in fields(cls)} - set(TRANSIENT_FIELDS)
        kwargs = {k: v for k, v in raw.items() if k in known}
        kwargs["segments"] = [Segment.from_list(s) for s in kwargs.get("segments", [])]
        try:
            kwargs["status"] = Status(kwargs.get("status", "queued"))
        except ValueError:
            kwargs["status"] = Status.PAUSED
        return cls(**kwargs)
