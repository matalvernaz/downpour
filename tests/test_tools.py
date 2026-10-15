"""Finding yt-dlp and FFmpeg in the places people keep them.

These are pure path checks: no yt-dlp or FFmpeg has to be installed, so unlike
test_media.py they run everywhere. The discovery helpers read the real registry
and winget folders, so the end-to-end tests stub those (and the PATH) out to
stay hermetic on a machine that happens to have the tools installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from downpour.core import media
from downpour.core.settings import Settings


def _fake(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("binary")
    return path


@pytest.fixture
def no_ambient(monkeypatch):
    """Nothing is found except through the arguments under test."""
    monkeypatch.setattr(media.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(media, "_registry_path_dirs", lambda: [])
    monkeypatch.setattr(media, "_winget_package_dirs", lambda base: [])


def test_as_exe_accepts_a_file_a_folder_or_nothing(tmp_path):
    exe = _fake(tmp_path / "bin" / "ffmpeg.exe")
    assert media._as_exe(str(exe), "ffmpeg.exe") == str(exe)
    assert media._as_exe(str(tmp_path / "bin"), "ffmpeg.exe") == str(exe)
    assert media._as_exe(str(tmp_path / "nowhere"), "ffmpeg.exe") is None
    assert media._as_exe("", "ffmpeg.exe") is None


def test_configured_file_wins(tmp_path):
    exe = _fake(tmp_path / "custom" / "ffmpeg.exe")
    assert media.find_ffmpeg(Settings(ffmpeg_path=str(exe))) == str(exe)


def test_configured_folder_works_for_both_tools(tmp_path):
    # The yt-dlp setting used to accept only a file; a folder must work too.
    folder = tmp_path / "bin"
    yt = _fake(folder / "yt-dlp.exe")
    ff = _fake(folder / "ffmpeg.exe")
    assert media.find_ytdlp(Settings(ytdlp_path=str(folder))) == str(yt)
    assert media.find_ffmpeg(Settings(ffmpeg_path=str(folder))) == str(ff)


def test_search_checks_dirs_in_order(tmp_path, no_ambient):
    first = _fake(tmp_path / "a" / "ffmpeg.exe")
    _fake(tmp_path / "b" / "ffmpeg.exe")
    dirs = [tmp_path / "a", tmp_path / "b"]
    assert media._search("", "ffmpeg", "ffmpeg.exe", dirs, []) == str(first)


def test_search_returns_none_when_nothing_matches(tmp_path, no_ambient):
    assert media._search("", "ffmpeg", "ffmpeg.exe", [tmp_path / "empty"], []) is None


def test_ffmpeg_found_in_a_guessed_folder(tmp_path, monkeypatch, no_ambient):
    exe = _fake(tmp_path / "pf" / "ffmpeg" / "bin" / "ffmpeg.exe")
    # Keep a stray C:\ffmpeg on the test machine from answering first.
    monkeypatch.setattr(media, "_ffmpeg_guess_dirs", lambda: [tmp_path / "pf" / "ffmpeg" / "bin"])
    assert media.find_ffmpeg(Settings()) == str(exe)


def test_path_beats_a_guessed_folder(tmp_path, monkeypatch):
    # A tool on the PATH must win over a stray copy in a common install folder.
    on_path = _fake(tmp_path / "real" / "ffmpeg.exe")
    _fake(tmp_path / "stray" / "ffmpeg.exe")
    monkeypatch.setattr(media, "_ffmpeg_own_dirs", list)
    monkeypatch.setattr(media, "_ffmpeg_guess_dirs", lambda: [tmp_path / "stray"])
    monkeypatch.setattr(media, "_registry_path_dirs", lambda: [])
    monkeypatch.setattr(media, "_winget_package_dirs", lambda base: [])
    monkeypatch.setattr(media.shutil, "which", lambda name: str(on_path) if name in ("ffmpeg", "ffmpeg.exe") else None)
    assert media.find_ffmpeg(Settings()) == str(on_path)


def test_each_tool_is_found_beside_the_other(tmp_path, monkeypatch, no_ambient):
    monkeypatch.setattr(media, "_ffmpeg_own_dirs", list)  # no other ffmpeg locations
    monkeypatch.setattr(media, "_ytdlp_own_dirs", list)
    monkeypatch.setattr(media, "_ffmpeg_guess_dirs", list)
    monkeypatch.setattr(media, "_ytdlp_guess_dirs", list)
    folder = tmp_path / "side"
    yt = _fake(folder / "yt-dlp.exe")
    ff = _fake(folder / "ffmpeg.exe")
    assert media.find_ffmpeg(Settings(ytdlp_path=str(yt))) == str(ff)
    assert media.find_ytdlp(Settings(ffmpeg_path=str(ff))) == str(yt)


def test_dir_lists_include_the_usual_places(monkeypatch):
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    ffmpeg = [str(d).lower() for d in media._ffmpeg_guess_dirs()]
    ytdlp = [str(d).lower() for d in media._ytdlp_guess_dirs()]
    assert any(d.endswith(r"\ffmpeg\bin") for d in ffmpeg)
    assert r"c:\program files\ffmpeg\bin" in ffmpeg
    assert any("winget" in d for d in ytdlp)


def test_audio_encoder_matches_the_container():
    assert media._audio_encoder(".mp4") == "aac"
    assert media._audio_encoder(".m4a") == "aac"
    assert media._audio_encoder(".webm") == "libopus"
    assert media._audio_encoder(".mkv") == "libopus"
    assert media._audio_encoder(".mp3") == "libmp3lame"
