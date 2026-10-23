import base64
from pathlib import Path

import pytest

from clipforge.media_sources import RemoteMedia
from clipforge.storage import PipelineError
from clipforge.youtube_auth import cookie_copy

COOKIES = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tprivate-test-value\n"


def test_private_cookie_copy_and_cleanup(tmp_path, monkeypatch):
    original = tmp_path / "cookies.txt"
    original.write_bytes(COOKIES)
    monkeypatch.setenv("YOUTUBE_COOKIES_B64", "invalid-but-explicit-file-wins")
    source = RemoteMedia(
        "https://youtu.be/abcdefghijk", tmp_path / "cache", cookies_file=original
    )
    copied = Path(source._options(True, "720p")["cookiefile"])
    assert copied != original
    assert copied.read_bytes() == COOKIES
    assert copied.stat().st_mode & 0o777 == 0o600
    copied.write_text("yt-dlp may update this file")
    source.close()
    assert not copied.exists()
    assert original.read_bytes() == COOKIES


def test_environment_cookie_only_used_for_youtube(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_COOKIES_B64", base64.b64encode(COOKIES).decode())
    youtube = RemoteMedia("https://youtu.be/abcdefghijk", tmp_path / "youtube")
    direct = RemoteMedia("https://example.com/audio.mp3", tmp_path / "direct")
    try:
        assert "cookiefile" in youtube._options(True, "720p")
        assert "cookiefile" not in direct._options(True, "720p")
    finally:
        youtube.close()
        direct.close()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not netscape private-test-value",
        COOKIES.replace(b".youtube.com", b".example.com"),
        COOKIES.replace(b"2000000000", b"bad-expiry"),
        COOKIES.replace(b"\tTRUE\t", b"\tINVALID\t"),
        b"\xff",
    ],
)
def test_reject_invalid_cookie_without_exposing_contents(data, monkeypatch):
    monkeypatch.setenv("YOUTUBE_COOKIES_B64", base64.b64encode(data).decode() or "!")
    with pytest.raises(PipelineError, match="invalid YouTube cookies") as error:
        cookie_copy()
    assert "private-test-value" not in str(error.value)


def test_symlink_rejected(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(COOKIES)
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(PipelineError):
        cookie_copy(link)
