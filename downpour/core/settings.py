"""User settings, persisted as JSON in the data directory.

Unknown keys in the file are ignored and missing keys take their defaults, so
settings files survive upgrades in both directions.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from downpour.core.paths import data_dir, downloads_dir

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# File types worth handing to Downpour instead of the browser. Mirrors the
# spirit of IDM's default capture list.
DEFAULT_CAPTURE_EXTENSIONS = (
    "3gp 7z aac ace aif aiff apk appx appxbundle arj asf avi bin bz2 cab deb dmg "
    "epub exe flac flv gz gzip img iso jar lz lzh lzma m4a m4b m4v mkv mov mp3 mp4 "
    "mpa mpe mpeg mpg msi msix msixbundle msu ogg ogv opus pdf pkg ppt pptx qt "
    "rar rm rmvb rpm sit sitx tar tbz tgz tif tiff ts txz vob wav webm wma wmv "
    "xz z zip zst"
).split()


@dataclass
class Category:
    name: str
    extensions: list[str]
    folder: str = ""  # empty = <download_dir>\<name>

    def matches(self, ext: str) -> bool:
        return ext.lower().lstrip(".") in self.extensions


def default_categories() -> list[Category]:
    return [
        Category("Compressed", "7z arj bz2 cab gz gzip lz lzh lzma rar tar tbz tgz txz xz z zip zst iso img".split()),
        Category("Documents", "csv doc docx epub md mobi odp ods odt pdf ppt pptx rtf txt xls xlsx azw3".split()),
        Category("Music", "aac aif aiff ape flac m4a m4b mid midi mp3 mpa ogg opus wav wma".split()),
        Category("Programs", "apk appx appxbundle bat deb dmg exe jar msi msix msixbundle msu pkg rpm".split()),
        Category("Video", "3gp asf avi flv m2ts m4v mkv mov mp4 mpe mpeg mpg ogv qt rm rmvb ts vob webm wmv".split()),
    ]


@dataclass
class Settings:
    # Saving
    download_dir: str = field(default_factory=lambda: str(downloads_dir()))
    use_category_folders: bool = True
    categories: list[Category] = field(default_factory=default_categories)
    duplicate_action: str = "rename"  # rename | overwrite | ask
    mark_of_the_web: bool = True

    # Connections
    max_connections: int = 8
    max_active: int = 3
    speed_limit: int = 0  # bytes per second, 0 = unlimited
    speed_limit_enabled: bool = False
    min_split_size: int = 1024 * 1024
    timeout: float = 30.0
    max_retries: int = 20
    user_agent: str = DEFAULT_USER_AGENT
    proxy_mode: str = "system"  # system | none | manual
    proxy_url: str = ""

    # Capture
    browser_capture: bool = True
    capture_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_CAPTURE_EXTENSIONS))
    capture_min_size: int = 0  # bytes; smaller downloads stay with the browser
    clipboard_monitor: bool = True
    show_info_dialog: bool = True

    # Feedback
    notify_complete: bool = True
    sound_complete: bool = True
    speak_events: bool = True
    show_complete_dialog: bool = False

    # Behaviour
    close_to_tray: bool = True
    start_with_windows: bool = False
    scan_after_download: bool = False
    when_done: str = "nothing"  # nothing | exit | sleep | hibernate | shutdown

    # Scheduler
    schedule_enabled: bool = False
    schedule_start: str = "02:00"
    schedule_stop: str = ""
    schedule_days: list[int] = field(default_factory=lambda: list(range(7)))  # 0 = Monday

    # Media (yt-dlp)
    media_format: str = "best-mp4"
    ytdlp_path: str = ""
    ffmpeg_path: str = ""

    def effective_speed_limit(self) -> int:
        return self.speed_limit if self.speed_limit_enabled else 0

    def category_for(self, filename: str) -> Category | None:
        ext = os.path.splitext(filename)[1]
        if not ext:
            return None
        for cat in self.categories:
            if cat.matches(ext):
                return cat
        return None

    def folder_for(self, filename: str) -> Path:
        base = Path(self.download_dir)
        if not self.use_category_folders:
            return base
        cat = self.category_for(filename)
        if cat is None:
            return base
        if cat.folder:
            return Path(cat.folder)
        return base / cat.name

    def wants_capture(self, filename: str, size: int | None) -> bool:
        if not self.browser_capture:
            return False
        if size is not None and 0 <= size < self.capture_min_size:
            return False
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        return ext in self.capture_extensions


_lock = threading.Lock()


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load() -> Settings:
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    return from_dict(raw)


def from_dict(raw: dict) -> Settings:
    s = Settings()
    known = {f.name for f in fields(Settings)}
    for key, value in raw.items():
        if key not in known:
            continue
        if key == "categories":
            try:
                value = [Category(**c) for c in value]
            except TypeError:
                continue
        default = getattr(s, key)
        if isinstance(default, bool) and not isinstance(value, bool):
            continue
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            value = type(default)(value)
        setattr(s, key, value)
    return s


def save(settings: Settings) -> None:
    path = settings_path()
    tmp = path.with_suffix(".tmp")
    data = json.dumps(asdict(settings), indent=2)
    with _lock:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
