"""Streams the browser saw a page play, as a media request's "found" list."""

from __future__ import annotations

from downpour.core import filenames
from downpour.core.media import found_streams, stream_downloads


def test_found_streams_keep_only_web_addresses_and_known_kinds():
    found = found_streams([
        {"url": "https://cdn.example.com/a/chapter1?sig=x", "kind": "file", "mime": "audio/mp4", "size": 52_000_000,
         "cookies": [{"name": "s", "value": "v", "domain": "cdn.example.com"}, "junk"]},
        {"url": "blob:https://example.com/123", "kind": "file"},
        {"url": "https://video.example.com/master.m3u8", "kind": "hls"},
        {"url": "https://x.example.com/y", "kind": "weird", "size": -1},
        "not a stream",
    ])
    assert [f.url for f in found] == [
        "https://cdn.example.com/a/chapter1?sig=x", "https://video.example.com/master.m3u8", "https://x.example.com/y",
    ]
    assert found[0].cookies == [{"name": "s", "value": "v", "domain": "cdn.example.com"}]
    assert found[1].is_playlist and not found[0].is_playlist
    assert found[2].kind == "file" and found[2].size is None


def test_labels_say_what_and_where():
    audio, playlist = found_streams([
        {"url": "https://cdn.example.com/a/chapter1", "mime": "audio/mp4", "size": 52_428_800},
        {"url": "https://video.example.com/master.m3u8", "kind": "hls"},
    ])
    assert audio.label() == "Audio, M4A, 50.0 MB, from cdn.example.com"
    assert playlist.label() == "HLS stream, played in pieces, from video.example.com"


def test_a_found_file_downloads_under_the_page_title_with_the_page_as_referrer():
    [audio] = found_streams([{"url": "https://cdn.example.com/stream/abc", "mime": "audio/mpeg", "size": 1000}])
    dl = audio.download("https://soundbooth.example/player/item/1", "Book 1 · Player", "UA/1")
    assert dl.filename == "Book 1 · Player.mp3" and dl.filename_fixed
    assert dl.referrer == dl.page_url == "https://soundbooth.example/player/item/1"
    assert dl.user_agent == "UA/1" and dl.size == 1000 and dl.mime == "audio/mpeg"


def test_media_extension_prefers_the_address_then_the_type():
    assert filenames.media_extension("https://x/a/track.ogg?sig=1", "audio/mpeg") == ".ogg"
    assert filenames.media_extension("https://x/stream/abc", "audio/x-m4a") == ".m4a"
    assert filenames.media_extension("https://x/stream/abc", "audio/flac") == ".flac"
    assert filenames.media_extension("https://x/stream/abc", "") == ""


def test_several_streams_download_in_play_order_with_numbers():
    newest_first = found_streams([
        {"url": "https://cdn.example.com/ch3", "mime": "audio/mp4"},
        {"url": "https://cdn.example.com/master.m3u8", "kind": "hls"},
        {"url": "https://cdn.example.com/ch1", "mime": "audio/mp4"},
    ])
    downloads = stream_downloads(newest_first, "https://example.com/book", "Book 1", "UA/1", "best-mp4")
    assert [d.url for d in downloads] == [
        "https://cdn.example.com/ch1", "https://cdn.example.com/master.m3u8", "https://cdn.example.com/ch3",
    ]
    assert [d.filename for d in downloads] == ["Book 1 - 1.m4a", "Book 1 - 2.mp4", "Book 1 - 3.m4a"]
    assert downloads[1].kind == "media" and downloads[1].media_format == "best-mp4"
    assert all(d.referrer == "https://example.com/book" for d in downloads)


def test_one_stream_keeps_the_plain_title():
    [only] = stream_downloads(found_streams([{"url": "https://cdn.example.com/a", "mime": "audio/mpeg"}]),
                              "https://example.com/p", "Book 1", "UA", "best-mp4")
    assert only.filename == "Book 1.mp3"


def test_audio_language_is_preferred_then_falls_back():
    from downpour.core.media import with_audio_language

    assert with_audio_language("bv*+ba/b", "") == "bv*+ba/b"
    assert with_audio_language("bv*+ba/b", "en") == "bv*+ba[language^=en]/bv*+ba/b"
    assert with_audio_language("ba/b", "original") == "ba/b"  # the default track is the original one
    assert with_audio_language("b", "fr") == "b"  # nothing to prefer in a single-file selector


def test_media_option_arguments():
    from downpour.core.media import PRESETS, media_option_args, preset

    video = preset("best-mp4")
    assert media_option_args({}, "ffmpeg.exe", video) == []
    args = media_option_args({"subtitles": "embed", "subtitle_language": "fr", "sponsorblock": True, "metadata": True,
                              "clip_start": "1:30", "clip_end": "12:00"}, "ffmpeg.exe", video)
    assert "--embed-subs" in args and "fr.*,fr,-live_chat" in args
    assert "--sponsorblock-remove" in args and "--embed-chapters" in args
    assert "*1:30-12:00" in args and "--force-keyframes-at-cuts" in args
    # Without FFmpeg nothing that needs it is asked for, but subtitle files still come.
    args = media_option_args({"subtitles": "file", "sponsorblock": True, "metadata": True, "clip_start": "5"}, None, video)
    assert "--write-subs" in args and "--sponsorblock-remove" not in args and "--download-sections" not in args
    # Audio-only downloads get the cover art; a transcript asks for SRT.
    args = media_option_args({"subtitles": "text", "metadata": True}, "ffmpeg.exe", preset("mp3"))
    assert "--embed-thumbnail" in args and "srt" in args
    assert any(c.audio_format == "flac" for c in PRESETS)


def test_transcript_drops_numbers_times_tags_and_repeats():
    from downpour.core.media import transcript_from_srt

    srt = "1\n00:00:01,000 --> 00:00:03,000\n<c>Hello</c> there\n\n2\n00:00:03,000 --> 00:00:05,000\nHello there\n\n3\n00:00:05,000 --> 00:00:07,000\nGeneral Kenobi\n"
    assert transcript_from_srt(srt) == "Hello there General Kenobi"
