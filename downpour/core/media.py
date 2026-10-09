"""Video and audio from web pages, through yt-dlp.

yt-dlp runs as a separate program rather than a bundled library. Sites change
every few weeks and yt-dlp has to be updated on its own schedule, not wait
for a Downpour release. Downpour looks for yt-dlp (a path in the settings,
then its own copy, then PATH) and can fetch the official build, checking it
against the published SHA-256 sums. FFmpeg, which yt-dlp needs to join
separate video and audio streams, is found and fetched the same way.

Progress comes from yt-dlp's machine-readable progress template, one JSON
object per line, so nothing depends on its human-readable output.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from downpour.core import filenames
from downpour.core.models import Download, Status
from downpour.core.paths import data_dir

if TYPE_CHECKING:
    from downpour.core.engine import Engine
    from downpour.core.settings import Settings

log = logging.getLogger(__name__)

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_SUMS = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
FFMPEG_ZIP = "ffmpeg-master-latest-win64-gpl-shared.zip"
FFMPEG_URL = f"https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/{FFMPEG_ZIP}"
FFMPEG_SUMS = "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/checksums.sha256"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MediaError(Exception):
    pass


# -- finding and installing the tools ------------------------------------------


def tools_dir() -> Path:
    path = data_dir() / "tools"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_ytdlp(settings: "Settings | None" = None) -> str | None:
    configured = getattr(settings, "ytdlp_path", "") if settings else ""
    for candidate in (configured, str(tools_dir() / "yt-dlp.exe")):
        if candidate and os.path.isfile(candidate):
            return candidate
    return shutil.which("yt-dlp")


def find_ffmpeg(settings: "Settings | None" = None) -> str | None:
    configured = getattr(settings, "ffmpeg_path", "") if settings else ""
    for candidate in (configured, str(tools_dir() / "ffmpeg" / "ffmpeg.exe")):
        if candidate and os.path.isfile(candidate):
            return candidate
        if candidate and os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "ffmpeg.exe")):
            return os.path.join(candidate, "ffmpeg.exe")
    return shutil.which("ffmpeg")


def _fetch(url: str, progress: Callable[[int, int | None], None] | None = None, stop: threading.Event | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Downpour"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = resp.headers.get("Content-Length")
        total_n = int(total) if total and total.isdigit() else None
        buf = io.BytesIO()
        while chunk := resp.read(256 * 1024):
            if stop is not None and stop.is_set():
                raise InterruptedError("cancelled")
            buf.write(chunk)
            if progress:
                progress(buf.tell(), total_n)
    return buf.getvalue()


def _expected_sum(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0].lower()
    raise MediaError(f"The published checksums do not list {name}")


def install_ytdlp(progress=None, stop=None) -> str:
    """Download the official yt-dlp.exe and verify it. Returns its path."""
    expected = _expected_sum(_fetch(YTDLP_SUMS).decode("utf-8", "replace"), "yt-dlp.exe")
    data = _fetch(YTDLP_URL, progress, stop)
    if hashlib.sha256(data).hexdigest() != expected:
        raise MediaError("The downloaded yt-dlp did not match its published checksum, so it was discarded.")
    target = tools_dir() / "yt-dlp.exe"
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return str(target)


def install_ffmpeg(progress=None, stop=None) -> str:
    """Download yt-dlp's recommended FFmpeg build and verify it."""
    expected = _expected_sum(_fetch(FFMPEG_SUMS).decode("utf-8", "replace"), FFMPEG_ZIP)
    data = _fetch(FFMPEG_URL, progress, stop)
    if hashlib.sha256(data).hexdigest() != expected:
        raise MediaError("The downloaded FFmpeg did not match its published checksum, so it was discarded.")
    target = tools_dir() / "ffmpeg"
    staging = Path(tempfile.mkdtemp(prefix="ffmpeg-", dir=tools_dir()))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            parts = Path(member.filename).parts
            # Keep only bin/* and never trust paths inside the archive.
            if len(parts) >= 3 and parts[-2] == "bin" and not member.is_dir():
                name = Path(parts[-1]).name
                (staging / name).write_bytes(archive.read(member))
    if not (staging / "ffmpeg.exe").exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise MediaError("The FFmpeg download did not contain ffmpeg.exe")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)
    return str(target / "ffmpeg.exe")


def update_ytdlp(path: str) -> str:
    """Run yt-dlp's self-update; returns what it said."""
    proc = subprocess.run([path, "-U"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW, timeout=300)
    return (proc.stdout + proc.stderr).strip()


# -- asking about a page ----------------------------------------------------------


@dataclass
class Choice:
    key: str
    label: str
    selector: str
    audio_only: bool = False
    needs_ffmpeg: bool = False
    extract_mp3: bool = False


PRESETS = [
    Choice("best-mp4", "Best quality video, MP4 (plays everywhere)",
           "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", needs_ffmpeg=True),
    Choice("best", "Best quality video, any format", "bv*+ba/b", needs_ffmpeg=True),
    Choice("1080", "Video up to 1080p", "bv*[height<=1080]+ba/b[height<=1080]/b", needs_ffmpeg=True),
    Choice("720", "Video up to 720p", "bv*[height<=720]+ba/b[height<=720]/b", needs_ffmpeg=True),
    Choice("480", "Video up to 480p", "bv*[height<=480]+ba/b[height<=480]/b", needs_ffmpeg=True),
    Choice("single", "Best single-file video (no FFmpeg needed)", "b"),
    Choice("audio", "Audio only, best quality, original format", "ba/b", audio_only=True),
    Choice("mp3", "Audio only, converted to MP3", "ba/b", audio_only=True, needs_ffmpeg=True, extract_mp3=True),
]


def preset(key: str) -> Choice:
    return next((c for c in PRESETS if c.key == key), PRESETS[0])


@dataclass
class MediaInfo:
    url: str
    title: str
    extractor: str = ""
    uploader: str = ""
    duration: float | None = None
    ext: str = ""
    size: int | None = None  # of the default format, when known
    formats: list[Choice] = field(default_factory=list)  # specific formats, best first
    entries: list[tuple[str, str]] = field(default_factory=list)  # playlist (url, title)

    @property
    def is_playlist(self) -> bool:
        return bool(self.entries)


def _cookie_file(cookies: list[dict]) -> str | None:
    """Write browser cookies in the Netscape format yt-dlp reads.

    The file holds secrets, so it lives in the private data folder and the
    caller deletes it as soon as yt-dlp has finished.
    """
    if not cookies:
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = str(c.get("domain", ""))
        if not domain:
            continue
        include_sub = "FALSE" if c.get("hostOnly") else "TRUE"
        if include_sub == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        expires = str(int(c["expirationDate"])) if c.get("expirationDate") else "0"
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([
            prefix + domain, include_sub, str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE", expires, str(c.get("name", "")), str(c.get("value", "")),
        ]))
    fd, path = tempfile.mkstemp(prefix="cookies-", suffix=".txt", dir=tools_dir())
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _common_args(dl_or_none, settings: "Settings", cookie_path: str | None, user_agent: str, referrer: str) -> list[str]:
    args = ["--no-warnings", "--no-colors", "--encoding", "utf-8"] + js_runtime_args()
    if cookie_path:
        args += ["--cookies", cookie_path]
    if user_agent:
        args += ["--user-agent", user_agent]
    if referrer:
        args += ["--referer", referrer]
    if settings.proxy_mode == "manual" and settings.proxy_url:
        args += ["--proxy", settings.proxy_url]
    elif settings.proxy_mode == "none":
        args += ["--proxy", ""]
    return args


def js_runtime_args() -> list[str]:
    """YouTube needs a JavaScript runtime. yt-dlp enables Deno by itself;
    when only Node or Bun is installed, enable that instead."""
    if shutil.which("deno"):
        return []
    for name in ("node", "bun"):
        path = shutil.which(name)
        if path:
            return ["--js-runtimes", f"{name}:{path}"]
    return []


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _last_error(text: str) -> str:
    errors = [line[len("ERROR:"):].strip() for line in text.splitlines() if line.startswith("ERROR:")]
    if errors:
        return errors[-1]
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "yt-dlp failed without saying why"


def _format_label(f: dict) -> str:
    parts = []
    if f.get("vcodec") not in (None, "none"):
        if f.get("height"):
            parts.append(f"{f['height']}p")
        if f.get("fps"):
            parts.append(f"{int(f['fps'])} fps")
        parts.append("video" if f.get("acodec") in (None, "none") else "video with audio")
    else:
        parts.append("audio")
        if f.get("abr"):
            parts.append(f"{int(f['abr'])} kbps")
    parts.append(str(f.get("ext") or "?").upper())
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        from downpour.core.units import format_size

        parts.append(format_size(size))
    if f.get("format_note"):
        parts.append(str(f["format_note"]))
    return ", ".join(parts)


def fetch_info(
    url: str,
    settings: "Settings",
    cookies: list[dict] | None = None,
    user_agent: str = "",
    referrer: str = "",
    playlist: bool = True,
    stop: threading.Event | None = None,
) -> MediaInfo:
    ytdlp = find_ytdlp(settings)
    if not ytdlp:
        raise MediaError("yt-dlp is not installed")
    cookie_path = _cookie_file(cookies or [])
    try:
        cmd = [ytdlp, "-J", "--flat-playlist" if playlist else "--no-playlist"]
        cmd += _common_args(None, settings, cookie_path, user_agent, referrer)
        cmd.append(url)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", creationflags=NO_WINDOW, env=_env())
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if stop is not None and stop.is_set():
                    _kill_tree(proc)
                    raise InterruptedError("cancelled")
    finally:
        if cookie_path:
            _remove(cookie_path)
    if proc.returncode != 0 or not out.strip():
        raise MediaError(_last_error(err))
    data = json.loads(out)
    return _parse_info(url, data)


def _parse_info(url: str, data: dict) -> MediaInfo:
    info = MediaInfo(
        url=data.get("webpage_url") or url,
        title=str(data.get("title") or data.get("id") or "video"),
        extractor=str(data.get("extractor_key") or data.get("extractor") or ""),
        uploader=str(data.get("uploader") or data.get("channel") or ""),
        duration=data.get("duration"),
        ext=str(data.get("ext") or ""),
    )
    if data.get("_type") == "playlist":
        for entry in data.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            entry_url = entry.get("url") or entry.get("webpage_url")
            if entry_url:
                info.entries.append((entry_url, str(entry.get("title") or entry_url)))
        return info
    requested = data.get("requested_formats") or [data]
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in requested]
    info.size = sum(sizes) if sizes and all(sizes) else None
    formats = [f for f in data.get("formats") or [] if isinstance(f, dict) and f.get("format_id")]
    # Usable, downloadable formats; storyboards and manifests are noise.
    formats = [f for f in formats if f.get("vcodec") != "none" or f.get("acodec") != "none"]
    formats = [f for f in formats if "storyboard" not in str(f.get("format_note", "")).lower()]
    formats.sort(key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)), reverse=True)
    for f in formats:
        video_only = f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
        audio_only = f.get("vcodec") in (None, "none")
        selector = f"{f['format_id']}+ba/{f['format_id']}" if video_only else str(f["format_id"])
        info.formats.append(Choice(
            key=f"id:{f['format_id']}", label=_format_label(f), selector=selector,
            audio_only=audio_only, needs_ffmpeg=video_only,
        ))
    return info


def suggested_name(info: MediaInfo, choice: Choice) -> str:
    if choice.extract_mp3:
        ext = "mp3"
    elif choice.key == "best-mp4":
        ext = "mp4"
    elif choice.audio_only:
        ext = "m4a" if info.ext in ("mp4", "m4a") else (info.ext or "m4a")
    else:
        ext = info.ext or "mp4"
    return filenames.sanitize(f"{info.title}.{ext}")


# -- downloading ------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop yt-dlp and any FFmpeg it started."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class MediaTask:
    """Runs one yt-dlp download with the same interface as HttpTask."""

    def __init__(self, engine: "Engine", dl: Download):
        self.engine = engine
        self.dl = dl
        self.stop_status = Status.PAUSED
        self.checkpointed = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._files: dict[str, tuple[int, int | None]] = {}
        self.thread = threading.Thread(target=self._run, name=f"media-{dl.id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self, status: Status = Status.PAUSED) -> None:
        self.stop_status = status
        self._stop.set()
        proc = self._proc
        if proc is not None:
            threading.Thread(target=_kill_tree, args=(proc,), daemon=True).start()

    def set_speed_limit(self, rate: int) -> None:
        pass  # yt-dlp takes its limit at start; it applies from the next resume

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _command(self, ytdlp: str, cookie_path: str | None) -> list[str]:
        dl = self.dl
        s = self.engine.settings
        choice = preset(dl.media_format) if not dl.media_format.startswith("id:") else None
        selector = choice.selector if choice else dl.media_format.split("|", 1)[-1]
        ffmpeg = find_ffmpeg(s)
        if choice and choice.needs_ffmpeg and not ffmpeg:
            selector = "ba/b" if choice.audio_only else "b"  # cannot merge streams without FFmpeg
        stem = Path(dl.filename).stem if dl.filename else "%(title).180B"
        template = str(Path(dl.directory) / f"{stem}.%(ext)s")
        cmd = [
            ytdlp, "--newline", "--progress", "--no-playlist", "--windows-filenames", "--no-mtime",
            "--progress-template", "download:DPPROG %(progress)j",
            "--print", "after_move:DPFILE %(filepath)s",
            "-f", selector, "-o", template, "-N", "4",
        ]
        cmd += _common_args(dl, s, cookie_path, dl.user_agent, dl.referrer)
        if ffmpeg:
            cmd += ["--ffmpeg-location", ffmpeg]
            if choice and choice.key == "best-mp4":
                cmd += ["--merge-output-format", "mp4"]
            if choice and choice.extract_mp3:
                cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        limit = dl.speed_limit or s.effective_speed_limit()
        if limit:
            cmd += ["--limit-rate", str(limit)]
        cmd.append(dl.url)
        return cmd

    def _run(self) -> None:
        from downpour.core.task import Outcome

        dl = self.dl
        outcome, error = Outcome.FAILED, ""
        cookie_path = None
        try:
            ytdlp = find_ytdlp(self.engine.settings)
            if not ytdlp:
                raise MediaError("yt-dlp is not installed. Open Options, Media, and install it.")
            Path(dl.directory).mkdir(parents=True, exist_ok=True)
            cookie_path = _cookie_file(dl.cookies)
            self.engine._set_status(dl, Status.CONNECTING)
            if self._stop.is_set():
                raise InterruptedError()
            self._proc = subprocess.Popen(
                self._command(ytdlp, cookie_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, env=_env(),
            )
            final_path, tail = self._follow(self._proc)
            code = self._proc.wait()
            if self._stop.is_set():
                outcome = Outcome.STOPPED
            elif code == 0:
                self._complete(final_path)
                outcome = Outcome.COMPLETED
            else:
                error = _last_error("\n".join(tail))
        except InterruptedError:
            outcome = Outcome.STOPPED
        except MediaError as e:
            error = str(e)
        except OSError as e:
            error = f"Could not run yt-dlp: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("media download %s crashed", dl.id)
            error = f"Unexpected error: {e}"
        finally:
            if cookie_path:
                _remove(cookie_path)
            dl.speed, dl.eta, dl.active_connections = 0.0, None, 0
        self.engine._task_done(self, outcome, error)

    def _follow(self, proc: subprocess.Popen) -> tuple[str, list[str]]:
        dl = self.dl
        final_path = ""
        tail: list[str] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith("DPPROG "):
                self._progress(line[7:])
            elif line.startswith("DPFILE "):
                final_path = line[7:].strip()
            else:
                tail = (tail + [line])[-40:]
                if line.startswith(("[Merger]", "[ExtractAudio]", "[FixupM3u8]", "[VideoConvertor]")):
                    if dl.status != Status.FINISHING:
                        self.engine._set_status(dl, Status.FINISHING)
        return final_path, tail

    def _progress(self, payload: str) -> None:
        try:
            p = json.loads(payload)
        except ValueError:
            return
        dl = self.dl
        name = str(p.get("filename") or p.get("tmpfilename") or "")
        total = p.get("total_bytes") or p.get("total_bytes_estimate")
        done = p.get("downloaded_bytes") or 0
        self._files[name] = (int(done), int(total) if total else None)
        dl.downloaded = sum(d for d, _ in self._files.values())
        totals = [t for _, t in self._files.values()]
        known = sum(t for t in totals if t)
        if dl.size is None or known > dl.size:
            dl.size = known or None
        dl.speed = float(p.get("speed") or 0.0)
        dl.eta = float(p["eta"]) if p.get("eta") is not None else None
        dl.active_connections = 1
        if dl.status != Status.DOWNLOADING and p.get("status") == "downloading":
            self.engine._set_status(dl, Status.DOWNLOADING)

    def _complete(self, final_path: str) -> None:
        dl = self.dl
        if final_path and os.path.exists(final_path):
            dl.directory, dl.filename = os.path.dirname(final_path), os.path.basename(final_path)
            dl.size = dl.downloaded = os.path.getsize(final_path)
        dl.completed_at = time.time()
        if self.engine.settings.mark_of_the_web and final_path:
            from downpour.core import net, winutil

            winutil.write_zone_identifier(final_path, net.safe_url(dl.url))
