import json
import sys
import threading
import time
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
            if url not in data:
                return SimpleNamespace(
                    status_code=404,
                    headers={},
                    iter_content=lambda: [],
                    close=lambda: None,
                )
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
    ("audio_only", "quality"), [(False, "preview"), (False, "1080p")]
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
    if quality == "preview":
        assert "-c:v" in command
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
    assert value.max_seconds == 43200
    with pytest.raises(PipelineError, match="budget"):
        value.fetch(MASTER, 1)
    value.max_seconds = 1
    with pytest.raises(PipelineError, match="duration budget"):
        value.playlist("lowest")
    value.max_seconds = 43200
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


@pytest.mark.parametrize("start,end", [(0, 4), (0.5, 3.25), (2.5, 3.5)])
def test_audio_retrieval_uses_pyav_without_cli(tmp_path, monkeypatch, start, end):
    from fractions import Fraction

    import av
    import numpy as np

    segments = []
    for index in range(2):
        path = tmp_path / f"synthetic-{index}.ts"
        with av.open(str(path), "w", format="mpegts") as output:
            audio = output.add_stream("aac", rate=48000)
            audio.layout = "stereo"
            for offset in range(0, 96000, 1024):
                count = min(1024, 96000 - offset)
                wave = (
                    0.2 * np.sin(2 * np.pi * 440 * np.arange(count) / 48000)
                ).astype("float32")
                frame = av.AudioFrame.from_ndarray(
                    np.stack([wave, wave]), format="fltp", layout="stereo"
                )
                frame.sample_rate, frame.time_base = 48000, Fraction(1, 48000)
                frame.pts = (100 + index * 2) * 48000 + offset
                output.mux(audio.encode(frame))
            output.mux(audio.encode(None))
        segments.append({"start": index * 2, "end": index * 2 + 2, "url": path})

    value = object.__new__(km.KickVOD)
    value.url = URL
    monkeypatch.setattr(value, "playlist", lambda quality: (segments, 0))
    # The audio path streams segments in order as they become ready.
    monkeypatch.setattr(
        value,
        "_stream_segments",
        lambda selected: (s["url"] for s in selected),
    )

    def no_cli(*args, **kwargs):
        pytest.fail("audio retrieval must not invoke a CLI")

    monkeypatch.setattr(km, "process", no_cli)
    monkeypatch.setenv("PATH", "")
    target = tmp_path / "audio.m4a"
    receipt = value.retrieve(target, start, end, audio_only=True)
    assert receipt == {
        "source_url": URL,
        "source_start": start,
        "source_end": end,
        "source_duration": 4,
        "rendition_height": 0,
        "audio_only": True,
        "segment_count": 1 if start >= 2 else 2,
        "sha256": km.digest(target),
    }
    with av.open(str(target)) as output:
        assert len(output.streams) == 1
        audio = output.streams.audio[0]
        assert audio.codec_context.name == "aac"
        assert audio.codec_context.sample_rate == 16000
        assert audio.codec_context.layout.name == "mono"
        assert audio.codec_context.bit_rate == pytest.approx(48000, rel=0.3)
        assert output.duration / av.time_base == pytest.approx(end - start, abs=0.1)
        frames = list(output.decode(audio))
        assert sum(frame.samples for frame in frames) / 16000 == pytest.approx(
            end - start, abs=0.15
        )
        assert max(float(np.abs(frame.to_ndarray()).max()) for frame in frames) > 0.01


API = "https://kick.com/api/v1/video/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
NO_VIDEO_METADATA = "<html><body>no embedded video metadata</body></html>"
MASTER_TEXT = (
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1920x1080\n1080.m3u8\n"
)


def api_payload(slug="example", source=MASTER):
    return {
        "source": source,
        "livestream": {
            "session_title": "Sample stream",
            "start_time": "2026-10-22T01:21:15+00:00",
            "duration": 17749000,
            "channel": {"slug": slug},
        },
    }


def json_ld(url=URL):
    return (
        '<script type="application/ld+json">'
        + json.dumps({"@type": "VideoObject", "url": url, "contentUrl": MASTER})
        + "</script>"
    )


@pytest.fixture
def fake_kick(monkeypatch):
    data, calls = {MASTER: MASTER_TEXT}, []

    class Session:
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append(url)
            if url not in data:
                return SimpleNamespace(
                    status_code=404,
                    headers={},
                    iter_content=lambda: [],
                    close=lambda: None,
                )
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
    return data, calls


def test_api_identifies_vod_without_page_metadata(tmp_path, fake_kick):
    data, calls = fake_kick
    data[URL] = NO_VIDEO_METADATA
    data[API] = json.dumps(api_payload())
    value = km.KickVOD(URL, tmp_path / "cache")
    try:
        assert value.master == MASTER
        assert value.metadata == {
            "name": "Sample stream",
            "duration": 17749.0,
            "uploadDate": "2026-10-22T01:21:15+00:00",
        }
        assert calls[0] == API
    finally:
        value.close()


def test_page_metadata_is_used_when_api_fails(tmp_path, fake_kick):
    data, calls = fake_kick
    data[URL] = json_ld()
    value = km.KickVOD(URL, tmp_path / "cache")
    try:
        assert value.master == MASTER
        assert API in calls
    finally:
        value.close()


@pytest.mark.parametrize(
    "slug,source",
    [
        ("someone-else", MASTER),
        (None, MASTER),
        ("example", "https://evil.test/archive/master.m3u8"),
        ("example", "http://stream.kick.com/archive/master.m3u8"),
        ("example", "https://stream.kick.com.evil.test/archive/master.m3u8"),
    ],
)
def test_api_identity_mismatch_fails_closed(tmp_path, fake_kick, slug, source):
    data, _ = fake_kick
    data[URL] = NO_VIDEO_METADATA
    data[API] = json.dumps(api_payload(slug=slug, source=source))
    with pytest.raises(PipelineError, match="identity"):
        km.KickVOD(URL, tmp_path / "cache")


def test_api_slug_match_ignores_channel_case(tmp_path, fake_kick):
    data, _ = fake_kick
    upper = URL.replace("/example/", "/Example/")
    data[upper] = NO_VIDEO_METADATA
    data[API] = json.dumps(api_payload(slug="EXAMPLE"))
    value = km.KickVOD(upper, tmp_path / "cache")
    try:
        assert value.master == MASTER
    finally:
        value.close()


def test_page_url_match_ignores_channel_case(tmp_path, fake_kick):
    data, _ = fake_kick
    upper = URL.replace("/example/", "/Example/")
    data[upper] = json_ld(url=URL)
    value = km.KickVOD(upper, tmp_path / "cache")
    try:
        assert value.master == MASTER
    finally:
        value.close()


def test_failed_api_and_page_keep_both_safe_reason_codes(vod, tmp_path, monkeypatch):
    def api(self):
        raise PipelineError("Kick media HTTP 403; failed after 1 attempts")

    def page(self):
        raise PipelineError(
            "Kick media HTTP 502; failed after 4 attempts https://private.example?secret=HIDDEN"
        )

    monkeypatch.setattr(km.KickVOD, "_api_video", api)
    monkeypatch.setattr(km.KickVOD, "_page_video", page)
    with pytest.raises(PipelineError) as failure:
        km.KickVOD(URL, tmp_path / "failed")
    assert failure.value.reasons == {"api": "http_403", "page": "http_502"}
    assert "api=http_403" in str(failure.value)
    assert "page=http_502" in str(failure.value)
    assert "HIDDEN" not in str(failure.value)
    assert "https://" not in str(failure.value)


def test_streamed_segments_yield_in_stream_order_even_when_finished_out_of_order(
    tmp_path,
):
    from concurrent.futures import ThreadPoolExecutor

    segments = [{"url": f"seg-{n}", "start": n, "end": n + 1} for n in range(6)]
    paths = {s["url"]: tmp_path / (s["url"] + ".ts") for s in segments}
    pool = ThreadPoolExecutor(max_workers=6)
    value = object.__new__(km.KickVOD)
    value.workers = 3
    value._lock = threading.RLock()

    def finish_later_for_earlier_segments(url):
        index = int(url.split("-")[1])
        time.sleep(0.02 * (len(segments) - index))
        return paths[url]

    value._segment_future = lambda url: pool.submit(
        finish_later_for_earlier_segments, url
    )
    try:
        assert list(value._stream_segments(segments)) == [
            paths[s["url"]] for s in segments
        ]
    finally:
        pool.shutdown(wait=True)


def test_streamed_download_failure_stops_the_stream_and_is_reported(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    segments = [{"url": f"seg-{n}", "start": n, "end": n + 1} for n in range(4)]
    pool = ThreadPoolExecutor(max_workers=4)
    value = object.__new__(km.KickVOD)
    value.workers = 2
    value._lock = threading.RLock()

    def fail_second(url):
        if url == "seg-1":
            raise km.PipelineError("segment failed")
        return tmp_path / (url + ".ts")

    value._segment_future = lambda url: pool.submit(fail_second, url)
    try:
        with pytest.raises(km.PipelineError, match="segment failed"):
            list(value._stream_segments(segments))
    finally:
        pool.shutdown(wait=True)


def test_extract_streamed_reports_a_download_failure_instead_of_short_audio(
    tmp_path, monkeypatch
):
    value = object.__new__(km.KickVOD)

    def stream(selected):
        yield from []
        raise km.PipelineError("segment failed mid-stream")

    monkeypatch.setattr(value, "_stream_segments", stream)
    with pytest.raises(km.PipelineError, match="segment failed mid-stream"):
        value._extract_streamed(
            [{"url": "x", "start": 0, "end": 1}],
            tmp_path / "audio.m4a",
            0.0,
            1.0,
        )
