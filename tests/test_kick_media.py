import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import kick_media as km
from clipforge.storage import PipelineError

URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
MASTER = "https://stream.kick.com/archive/master.m3u8"
PLAYLIST = "#EXTM3U\n#EXTINF:10,\n0.ts\n#EXTINF:10,\n1.ts\n#EXT-X-ENDLIST\n"


@pytest.fixture
def vod(tmp_path, monkeypatch):
    data = {
        URL: '<script type="application/ld+json">'
        + json.dumps({"@type": "VideoObject", "url": URL, "contentUrl": MASTER})
        + "</script>",
        MASTER: '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=284x160\n160.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1920x1080\n1080.m3u8\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\n',
        **{
            f"https://stream.kick.com/archive/{name}.m3u8": PLAYLIST
            for name in ("160", "1080", "audio")
        },
        **{
            f"https://stream.kick.com/archive/{n}.ts": b"G" + b"synthetic" * 10
            for n in range(2)
        },
    }
    calls = []

    class Session:
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append(url)
            body = data[url]
            body = body.encode() if isinstance(body, str) else body
            return SimpleNamespace(
                status_code=200, iter_content=lambda: [body], close=lambda: None
            )

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "curl_cffi",
        SimpleNamespace(requests=SimpleNamespace(Session=Session)),
    )
    value = km.KickVOD(URL, tmp_path / "cache")
    commands = []
    duration = [0]

    def process(command, **kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            duration[0] = (
                float(command[command.index("-t") + 1])
                if "-t" in command
                else Path(command[command.index("-i") + 1]).stat().st_size / 91 * 10
            )
            Path(command[-1]).write_bytes(b"synthetic rendered media")
            return SimpleNamespace(stdout=b"")
        if "-select_streams" in command:
            return SimpleNamespace(
                stdout=json.dumps({"streams": [{"start_time": 100}]})
            )
        return SimpleNamespace(stdout=json.dumps({"format": {"duration": duration[0]}}))

    monkeypatch.setattr(km, "process", process)
    yield value, data, calls, commands
    value.close()


def test_renditions_keep_audio_first_and_preview_video_distinct(vod):
    value, _, _, _ = vod
    assert value.playlist("lowest")[1] == 0
    assert value.playlist("preview")[1] == 160
    assert value.playlist("1080p")[1] == 1080
    assert value.playlist("720p")[1] == 160
    with pytest.raises(PipelineError):
        value.playlist("invalid")


@pytest.mark.parametrize(
    ("audio_only", "quality"), [(True, "720p"), (False, "preview"), (False, "1080p")]
)
def test_retrieve_range_uses_original_clock_and_verifiable_cache(
    vod, tmp_path, audio_only, quality
):
    value, _, calls, commands = vod
    target = tmp_path / ("audio.m4a" if audio_only else "video.mp4")
    result = value.retrieve(target, 12.5, 17, audio_only=audio_only, quality=quality)
    assert result["source_start"] == 12.5 and result["source_end"] == 17
    assert result["audio_only"] is audio_only
    assert result["segment_count"] == 1
    command = next(c for c in commands if c[0] == "ffmpeg")
    if audio_only or quality == "preview":
        assert "-vn" in command if audio_only else "-c:v" in command
        assert command[command.index("-ss") + 1] == "2.5"
        assert "media_format" not in result
    else:
        assert command[command.index("-c") + 1] == "copy"
        assert "-ss" not in command and "-t" not in command
        assert "libx264" not in command and "aac" not in command
        assert command[command.index("-itsoffset") + 1] == "-100.0"
        assert command[command.index("-avoid_negative_ts") + 1] == "disabled"
        assert "+genpts" in command and "-copyts" in command
        assert result["media_format"] == km.RANGE_FORMAT
        assert result["start_offset"] == 2.5
        assert result["covered_duration"] == 10.0
    before = len(calls)
    value.segment("https://stream.kick.com/archive/1.ts")
    assert len(calls) == before
    with pytest.raises(PipelineError, match="exists"):
        value.retrieve(target)
    with pytest.raises(PipelineError, match="outside"):
        value.retrieve(tmp_path / "other.mp4", 30, 35)


@pytest.mark.parametrize(
    ("start", "end", "offset", "covered", "count"),
    [(10, 20, 0, 10, 1), (12.5, 20, 2.5, 10, 1), (2.5, 17, 2.5, 20, 2)],
)
def test_remux_receipt_covers_whole_selected_segments(
    vod, tmp_path, start, end, offset, covered, count
):
    result = vod[0].retrieve(tmp_path / "video.mp4", start, end, quality="1080p")
    assert result["start_offset"] == offset
    assert result["covered_duration"] == covered
    assert result["segment_count"] == count


def test_remux_rejects_incomplete_segment_coverage(vod, tmp_path, monkeypatch):
    original = km.process

    def process(command, **kwargs):
        if "format=duration" in command:
            return SimpleNamespace(stdout=json.dumps({"format": {"duration": 5}}))
        return original(command, **kwargs)

    monkeypatch.setattr(km, "process", process)
    target = tmp_path / "video.mp4"
    with pytest.raises(PipelineError, match="duration mismatch"):
        vod[0].retrieve(target, 12, 17, quality="1080p")
    assert not target.exists()


@pytest.mark.parametrize(
    "reference",
    [
        "https://evil.test/a.ts",
        "../secret.ts",
        "x.ts?token=x",
        "%2e%2e/x.ts",
        "x.ts#fragment",
        "/different/x.ts",
    ],
)
def test_media_paths_stay_inside_public_archive(reference):
    with pytest.raises(PipelineError):
        km.child(MASTER, reference, "/archive/")


@pytest.mark.parametrize(
    "tag",
    [
        "#EXT-X-KEY",
        "#EXT-X-MAP",
        "#EXT-X-BYTERANGE",
        "#EXT-X-DISCONTINUITY",
        "not-hls",
        "#EXTM3U\na.ts",
        "#EXTM3U\n#EXTINF:nan,\na.ts",
    ],
)
def test_unsupported_playlists_fail_closed(tag):
    text = tag if tag.startswith("#EXTM3U") or tag == "not-hls" else "#EXTM3U\n" + tag
    with pytest.raises(PipelineError):
        km.parse_media(text, MASTER, "/archive/")


def test_byte_duration_and_live_stream_limits(vod):
    value, data, _, _ = vod
    with pytest.raises(PipelineError, match="budget"):
        value.fetch(MASTER, 1)
    value.max_seconds = 1
    with pytest.raises(PipelineError, match="duration budget"):
        value.playlist("lowest")
    value.max_seconds = 14400
    data["https://stream.kick.com/archive/audio.m3u8"] = PLAYLIST.replace(
        "#EXT-X-ENDLIST", ""
    )
    with pytest.raises(PipelineError, match="still growing"):
        value.playlist("lowest")


def test_invalid_source_and_unadvertised_video(tmp_path, vod):
    with pytest.raises(PipelineError):
        km.KickVOD("https://evil.test/input", tmp_path)
    with pytest.raises(PipelineError):
        km.KickVOD(URL, tmp_path, max_bytes=1)
    value = vod[0]
    value.variants = [v for v in value.variants if v["height"] == 0]
    for quality in ("preview", "1080p"):
        with pytest.raises(PipelineError):
            value.playlist(quality)


def test_prefetch_downloads_only_the_interval_over_http_and_retrieve_reuses_it(
    vod, tmp_path
):
    value, _, calls, commands = vod
    value.prefetch(12, 17)
    assert commands == []
    segments = [c for c in calls if c.endswith(".ts")]
    assert segments == ["https://stream.kick.com/archive/1.ts"]
    value.retrieve(tmp_path / "video.mp4", 12, 17)
    assert [c for c in calls if c.endswith(".ts")] == segments


def test_prefetch_refuses_an_interval_outside_the_archive(vod):
    value, _, calls, _ = vod
    with pytest.raises(PipelineError, match="outside"):
        value.prefetch(15, 30)
    assert not [c for c in calls if c.endswith(".ts")]


def test_concurrent_prefetch_and_segment_never_download_a_url_twice(vod):
    value, _, calls, _ = vod
    url = "https://stream.kick.com/archive/0.ts"
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(value.prefetch, 0, 20) for _ in range(4)]
        futures += [pool.submit(value.segment, url) for _ in range(4)]
        for future in futures:
            future.result()
    segments = [c for c in calls if c.endswith(".ts")]
    assert sorted(segments) == sorted(set(segments))
    assert sorted(segments) == [
        "https://stream.kick.com/archive/0.ts",
        "https://stream.kick.com/archive/1.ts",
    ]
