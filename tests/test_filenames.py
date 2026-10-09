from pathlib import Path

from downpour.core import checksum, filenames
from downpour.core.units import format_duration, format_size, parse_size


def test_disposition_plain():
    assert filenames.from_content_disposition('attachment; filename="report.pdf"') == "report.pdf"


def test_disposition_rfc5987_wins():
    value = "attachment; filename=\"fallback.txt\"; filename*=UTF-8''na%C3%AFve%20file.txt"
    assert filenames.from_content_disposition(value) == "naïve file.txt"


def test_disposition_raw_utf8_mojibake_is_repaired():
    raw = 'attachment; filename="café.zip"'.encode("utf-8").decode("latin-1")
    assert filenames.from_content_disposition(raw) == "café.zip"


def test_disposition_strips_paths():
    assert filenames.from_content_disposition('attachment; filename="..\\..\\evil.exe"') == "evil.exe"


def test_disposition_absent():
    assert filenames.from_content_disposition("inline") is None
    assert filenames.from_content_disposition(None) is None


def test_sanitize():
    assert filenames.sanitize('a<b>c:"d"|e?.txt') == "a_b_c__d__e_.txt"
    assert filenames.sanitize("CON.txt") == "_CON.txt"
    assert filenames.sanitize("trailing dots...") == "trailing dots"
    assert filenames.sanitize("..") == "download"
    assert len(filenames.sanitize("x" * 400 + ".zip")) <= filenames.MAX_NAME


def test_choose_from_url_and_mime():
    assert filenames.choose("https://e.com/files/My%20Song.mp3?x=1", None, "audio/mpeg") == "My Song.mp3"
    assert filenames.choose("https://e.com/get.php?id=4", None, "application/zip") == "get.zip"
    assert filenames.choose("https://e.com/", None, "") == "e.com"


def test_unique_path(tmp_path: Path):
    target = tmp_path / "a.zip"
    assert filenames.unique_path(target) == target
    target.write_bytes(b"x")
    assert filenames.unique_path(target).name == "a (2).zip"
    (tmp_path / "a (2).zip.part").write_bytes(b"")
    assert filenames.unique_path(target).name == "a (3).zip"
    taken = {str(tmp_path / "a (3).zip").lower()}
    assert filenames.unique_path(target, taken).name == "a (4).zip"


def test_checksum_parse():
    md5 = "d41d8cd98f00b204e9800998ecf8427e"
    assert checksum.parse(md5) == ("md5", md5)
    assert checksum.parse("SHA-256: " + "A" * 64) == ("sha256", "a" * 64)
    assert checksum.parse("sha1:" + "0" * 64) is None  # length disagrees with the name
    assert checksum.parse("not a hash") is None


def test_units():
    assert format_size(1536) == "1.5 KB"
    assert format_duration(125) == "2 minutes 5 seconds"
    assert format_duration(3725) == "1 hour 2 minutes"
    assert parse_size("1.5 MB") == 1572864
    assert parse_size("300 KB/s") == 307200
    assert parse_size("fast") is None
