"Shared helpers."

import json
import ssl
import subprocess
import threading
import pytest
import hashlib
import importlib.util
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import HTTPSHandler, build_opener
from clipforge import audio_first, media_sources as sources
from clipforge.storage import PipelineError
from pathlib import Path
from clipforge.media_sources import verify_audio_artifact


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://cdn.example/source.mp4?signature=secret", "direct"),
        ("https://cdn.example/audio.M4A", "direct"),
        ("https://cdn.example/audio.mp3", "direct"),
        ("https://cdn.example/source.webm", "direct"),
        ("https://cdn.example/master.m3u8?signature=secret", "direct"),
        ("https://youtu.be/abcdefghijk", "youtube"),
        ("https://www.youtube.com/watch?v=abcdefghijk&t=20", "youtube"),
        ("https://youtube.com/shorts/abcdefghijk", "youtube"),
        ("https://www.twitch.tv/videos/12345", "twitch"),
        ("https://clips.twitch.tv/Example-clip", "twitch"),
        ("https://www.twitch.tv/example/clip/Example-clip", "twitch"),
    ],
)
def test_supported_single_sources_preserve_url_identity(url, kind):
    assert sources.source_kind(url) == kind
    assert sources.canonical(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/audio.mp3",
        "file:///tmp/audio.mp3",
        "https://user:password@cdn.example/audio.mp3",
        "https://cdn.example/audio.mp3#fragment",
        "https://cdn.example/page.html",
        "https://youtube.com/playlist?list=123",
        "https://youtube.com/channel/123",
        "https://youtu.be/invalid",
        "https://youtube.com.evil.example/watch?v=abcdefghijk",
        "https://twitch.tv/livechannel",
        "https://cdn.example:invalid/audio.mp3",
        "https://cdn.example/audio.mp3\n",
    ],
)
def test_invalid_sources_fail_before_network(url):
    with pytest.raises(PipelineError):
        sources.canonical(url)


@pytest.fixture(scope="module")
def https_media(tmp_path_factory):
    root = tmp_path_factory.mktemp("https-media")

    def command(*arguments):
        subprocess.run(arguments, check=True, capture_output=True, timeout=30)

    command(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(root / "key.pem"),
        "-out",
        str(root / "cert.pem"),
        "-days",
        "1",
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost,IP:127.0.0.1",
    )
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x180:r=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000",
        "-t",
        "3",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-g",
        "24",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(root / "source.mp4"),
    )
    for suffix, codec in (("m4a", "aac"), ("mp3", "libmp3lame")):
        command(
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(root / "source.mp4"),
            "-vn",
            "-c:a",
            codec,
            str(root / ("source." + suffix)),
        )
    command(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(root / "source.mp4"),
        "-c:v",
        "libvpx-vp9",
        "-cpu-used",
        "8",
        "-threads",
        "1",
        "-c:a",
        "libopus",
        str(root / "source.webm"),
    )
    command(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(root / "source.mp4"),
        "-c",
        "copy",
        "-hls_time",
        "1",
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        str(root / "segment%03d.ts"),
        str(root / "source.m3u8"),
    )
    (root / "live.m3u8").write_text(
        (root / "source.m3u8").read_text().replace("#EXT-X-ENDLIST", "")
    )

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *arguments):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(root))
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def trusted_server(https_media, monkeypatch):
    import yt_dlp

    # Trust only the test server's generated certificate for this loopback integration.
    base, root = https_media
    context = ssl.create_default_context(cafile=str(root / "cert.pem"))
    monkeypatch.setattr(
        sources,
        "build_opener",
        lambda *handlers: build_opener(*handlers, HTTPSHandler(context=context)),
    )
    original = yt_dlp.YoutubeDL

    def downloader(options):
        return original({**options, "nocheckcertificate": True, "proxy": ""})

    monkeypatch.setattr(yt_dlp, "YoutubeDL", downloader)
    return base


@pytest.mark.parametrize("suffix", ["mp4", "m4a", "mp3", "webm", "m3u8"])
def test_owned_https_media_reaches_saved_audio_without_asr(
    trusted_server, tmp_path, suffix, capsys
):
    url = trusted_server + "/source." + suffix + "?signature=private-test"
    source = sources.open_source(
        url, tmp_path / "transport", max_bytes=2 * 1024**2, max_seconds=10
    )
    try:
        receipt = audio_first.acquire(source, tmp_path)
        assert receipt["source_url"] == url
        assert 2.9 < audio_first.audio_info(tmp_path / "audio.m4a")["duration"] < 3.3
        assert 0 < source.used <= source.max_bytes
        assert not json.loads((tmp_path / "acquisition.json").read_text())[
            "asr_started"
        ]
        if suffix in ("mp3", "m4a"):
            assert source.has_video is False
        with pytest.raises(PipelineError, match="exists"):
            source.retrieve(tmp_path / "audio.m4a", audio_only=True)
    finally:
        source.close()
    assert not list((tmp_path / "transport").iterdir())
    captured = capsys.readouterr()
    assert "private-test" not in captured.out + captured.err


def test_live_hls_and_transport_limits_stop_before_asr(trusted_server, tmp_path):
    live = sources.RemoteMedia(trusted_server + "/live.m3u8", tmp_path / "live")
    limited = sources.RemoteMedia(
        trusted_server + "/source.mp4", tmp_path / "limited", max_bytes=1024
    )
    try:
        with pytest.raises(PipelineError, match="finite|live"):
            live.metadata()
        with pytest.raises(PipelineError, match="budget|missing|oversized"):
            limited.retrieve(tmp_path / "audio.m4a", audio_only=True)
        assert not (tmp_path / "audio.m4a").exists()
    finally:
        live.close()
        limited.close()


def test_video_preview_and_remux_receipts_keep_original_coordinates(
    trusted_server, tmp_path
):
    source = sources.RemoteMedia(trusted_server + "/source.mp4", tmp_path / "transport")
    try:
        preview = source.retrieve(
            tmp_path / "preview.mp4", start=0.5, end=2, quality="preview"
        )
        assert preview["source_start"] == 0.5 and preview["source_end"] == 2
        receipt = source.retrieve(
            tmp_path / "full.mp4", start=0.5, end=2, quality="1080p"
        )
        assert receipt["media_format"] == sources.RANGE_FORMAT
        assert receipt["start_offset"] == 0.5
        assert receipt["covered_duration"] >= 3
    finally:
        source.close()


def test_audio_sources_refuse_video_stages(trusted_server, tmp_path):
    source = sources.RemoteMedia(trusted_server + "/source.mp3", tmp_path / "transport")
    try:
        with pytest.raises(PipelineError, match="audio-only"):
            source.retrieve(tmp_path / "preview.mp4", start=0, end=2, quality="preview")
    finally:
        source.close()


@pytest.mark.parametrize(
    "url",
    ["https://www.youtube.com/watch?v=abcdefghijk", "https://www.twitch.tv/videos/123"],
)
def test_platform_metadata_uses_yt_dlp_without_downloading_or_forwarding_credentials(
    tmp_path, monkeypatch, url
):
    import yt_dlp

    calls = []

    class Downloader:
        def __init__(self, options):
            assert options["noplaylist"] and options["retries"] == 0
            assert options["js_runtimes"] == {"node": {}}
            assert "username" not in options and "cookiefile" not in options

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            pass

        def extract_info(self, target, download):
            calls.append((target, download))
            return {
                "url": "https://cdn.example/audio.m4a",
                "protocol": "https",
                "duration": 60,
                "vcodec": "none",
                "formats": [{"vcodec": "avc1", "height": 720}],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    source = sources.open_source(url, tmp_path / "media")
    try:
        assert source.metadata() == {"duration": 60, "has_video": True}
    finally:
        source.close()
    assert calls == [(url, False)]


def test_provider_errors_keep_reason_and_redact_urls_and_credentials():
    detail = sources.provider_error(
        Exception(
            "\x1b[31mSign in to confirm you are not a bot: https://cdn.example/file?signature=secret token=hidden\nAuthorization: Bearer private-value\nCookie: session=private-cookie"
        )
    )
    assert "Sign in to confirm you are not a bot" in detail
    assert all(
        secret not in detail
        for secret in (
            "secret",
            "hidden",
            "private-value",
            "private-cookie",
            "https://",
        )
    )


@pytest.mark.parametrize("metadata_only", [True, False])
def test_original_yt_dlp_failure_identifies_the_stage_without_a_second_attempt(
    tmp_path, monkeypatch, metadata_only
):
    import yt_dlp
    from yt_dlp.utils import DownloadError

    calls = []

    class Downloader:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            calls.append(download)
            raise DownloadError(
                "ERROR: [youtube] video: Sign in to confirm you are not a bot. https://example.test/?signature=private-value"
            )

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    source = sources.RemoteMedia("https://youtu.be/6ras3m-gFuc", tmp_path / "media")
    try:
        with pytest.raises(PipelineError, match="Sign in to confirm") as caught:
            if metadata_only:
                source.metadata()
            else:
                source.retrieve(tmp_path / "audio.m4a", audio_only=True)
        assert "private-value" not in str(caught.value)
        if not metadata_only:
            assert "phase=metadata" in str(caught.value)
    finally:
        source.close()
    assert calls == [False]


def test_youtube_web_profile_requests_po_tokens_only_when_enabled(tmp_path):
    default = sources.RemoteMedia("https://youtu.be/abcdefghijk", tmp_path / "default")
    web = sources.RemoteMedia(
        "https://youtu.be/abcdefghijk", tmp_path / "web", youtube_web_clients=True
    )
    try:
        assert "extractor_args" not in default._options(True, "lowest")
        assert web._options(True, "lowest")["extractor_args"]["youtube"] == {
            "player_client": ["web", "mweb"],
            "fetch_pot": ["always"],
            "webpage_skip": ["player_response"],
        }
        with pytest.raises(PipelineError, match="requires a YouTube source"):
            sources.RemoteMedia(
                "https://example.com/file.mp3",
                tmp_path / "direct",
                youtube_web_clients=True,
            )
    finally:
        default.close()
        web.close()


URL = "https://youtu.be/6ras3m-gFuc"


@pytest.fixture
def artifact(tmp_path):
    root = tmp_path / "verified"
    root.mkdir()
    audio = b"verified-private-test-audio"
    (root / "audio.m4a").write_bytes(audio)
    manifest = {
        "schema": 1,
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "workflow_run": 123,
        "source_url": URL,
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "audio_bytes": len(audio),
        "audio_info": {"audio": True, "duration": 10.0, "audio_start": 0},
        "source_has_video": True,
        "asr_started": False,
    }
    (root / "audio-source.json").write_text(json.dumps(manifest))
    expected = {
        "source_url": URL,
        "source_ref": "b" * 40,
        "request_id": "a" * 32,
        "workflow_run": 123,
        "max_bytes": 4096,
        "max_seconds": 20,
    }
    return root, manifest, expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "c" * 32),
        ("source_ref", "c" * 40),
        ("workflow_run", 456),
        ("source_url", "https://youtu.be/abcdefghijk"),
        ("audio_sha256", "0" * 64),
        ("audio_bytes", 0),
        ("schema", True),
        ("asr_started", True),
    ],
)
def test_artifact_tamper_and_identity_mismatch_are_rejected(artifact, field, value):
    root, manifest, expected = artifact
    manifest[field] = value
    (root / "audio-source.json").write_text(json.dumps(manifest))
    with pytest.raises(PipelineError, match="mismatch"):
        verify_audio_artifact(root, **expected)


def test_artifact_requires_integrity_duration_budget_and_regular_files(artifact):
    root, manifest, expected = artifact
    assert verify_audio_artifact(root, **expected) == manifest
    with pytest.raises(PipelineError):
        verify_audio_artifact(root, **{**expected, "max_seconds": 5})
    (root / "audio.m4a").write_bytes(b"tampered-but-valid-sizedxxx")
    with pytest.raises(PipelineError):
        verify_audio_artifact(root, **expected)
    (root / "audio.m4a").unlink()
    (root / "audio.m4a").symlink_to(root / "audio-source.json")
    with pytest.raises(PipelineError, match="regular files"):
        verify_audio_artifact(root, **expected)


def test_reuse_stages_new_identity_without_network_or_transport(
    artifact, tmp_path, monkeypatch
):
    root, manifest, _ = artifact
    repository = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(repository / "tools"))
    spec = importlib.util.spec_from_file_location(
        "acquire_audio_for_reuse", repository / "tools/acquire_audio_url.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        raise AssertionError("reuse must never acquire a URL or initialize WARP")

    monkeypatch.setattr(module, "open_source", forbidden)
    from clipforge import youtube_transport

    monkeypatch.setattr(youtube_transport, "verify_ready", forbidden)
    output = tmp_path / "staged"
    module.main(
        [
            "--url",
            URL,
            "--request-id",
            "c" * 32,
            "--source-ref",
            "d" * 40,
            "--settings-json",
            json.dumps(
                {
                    "gemini_model": "manual-no-api",
                    "asr_parts": 2,
                    "youtube_audio_transport": "warp",
                }
            ),
            "--directory",
            str(output),
            "--verified-audio-dir",
            str(root),
            "--verified-audio-run",
            "123",
            "--verified-audio-request",
            "a" * 32,
            "--verified-audio-source-ref",
            "b" * 40,
        ]
    )
    request = json.loads((output / "request.json").read_text())
    receipt = json.loads((output / "acquisition-report.json").read_text())
    assert request["request_id"] == "c" * 32
    assert request["source_ref"] == "d" * 40
    assert request["video_sha256"] == manifest["audio_sha256"]
    assert (output / "input-video.bin").read_bytes() == (
        root / "audio.m4a"
    ).read_bytes()
    assert (
        receipt["transport"] == "verified_artifact" and receipt["producer_run"] == 123
    )
    assert receipt["asr_started"] is False
