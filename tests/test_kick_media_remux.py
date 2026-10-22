"""Small synthetic TS regression for the remux and final-encode clocks."""

import json
from pathlib import Path
import shutil

import pysubs2
import pytest

from clipforge import audio_first, kick_media, media, pipeline, quality
from clipforge.models import SelectedClip, Transcript
from clipforge.storage import process


@pytest.mark.integration
@pytest.mark.parametrize("silence_cut", [False, True])
def test_synthetic_ts_remux_and_final_cut(tmp_path, monkeypatch, settings, silence_cut):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for the synthetic TS regression")
    process(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-filter_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=25:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "50",
            "-keyint_min",
            "50",
            "-sc_threshold",
            "0",
            "-bf",
            "2",
            "-c:a",
            "aac",
            "-f",
            "segment",
            "-segment_time",
            "2",
            "-segment_format",
            "mpegts",
            str(tmp_path / "%d.ts"),
        ]
    )
    segments = [
        {"start": n * 2.0, "end": (n + 1) * 2.0, "url": str(tmp_path / f"{n}.ts")}
        for n in range(3)
    ]
    vod = object.__new__(kick_media.KickVOD)
    vod.url = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
    monkeypatch.setattr(vod, "playlist", lambda quality: (segments, 1080))
    # Use paths, as the real transport does, while retaining original TS timestamps.
    monkeypatch.setattr(
        vod, "_download_segments", lambda selected: [Path(s["url"]) for s in selected]
    )
    target = tmp_path / "range.mp4"
    requested_start, requested_end = 2.48, 5.0
    receipt = vod.retrieve(target, requested_start, requested_end, quality="1080p")
    assert receipt["start_offset"] == pytest.approx(0.48)
    assert receipt["covered_duration"] == 4.0
    probe = json.loads(
        process(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                target,
            ]
        ).stdout
    )
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert float(video["start_time"]) == pytest.approx(0, abs=0.0001)
    assert media.probe(target)["duration"] == pytest.approx(4.0, abs=0.1)

    original = tmp_path / "selected.ts"
    original.write_bytes(b"".join((tmp_path / f"{n}.ts").read_bytes() for n in (1, 2)))

    # MPEG-TS Annex B and MP4 AVCC packet framing differs; decoding must be lossless.
    def decoded_hashes(path):
        return process(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                path,
                "-map",
                "0:v:0",
                "-f",
                "framemd5",
                "-",
            ]
        ).stdout

    assert decoded_hashes(original) == decoded_hashes(target)
    transcript = Transcript.model_validate(
        {
            "language": "en",
            "segments": [
                {
                    "start": 2.8,
                    "end": 4.8,
                    "text": "Synthetic caption",
                    "words": [{"start": 2.8, "end": 4.8, "text": "Synthetic caption"}],
                }
            ],
        }
    )
    offset = receipt["start_offset"]
    local = SelectedClip(
        start=offset,
        end=offset + (requested_end - requested_start),
        candidate_id="synthetic",
        title="Synthetic",
        reason="Synthetic TS clock regression",
        confidence=1,
    )
    shifted = audio_first.shifted_transcript(
        transcript, requested_start, requested_end, offset=offset
    )
    settings.aspect = "16:9"
    settings.layout = "fit"
    settings.font = "DejaVu Sans"
    settings.encode_threads = 1
    settings.video_encoder = "libx264"
    settings.silence_cut = silence_cut
    settings.silence_pad = 0.0
    settings.silence_min_gap = 0.0
    settings.fade_seconds = 0.0
    settings.progress_bar = False
    settings.quality_gate = "report"
    # Keep this regression tiny even though delivery normally uses 720p or higher.
    monkeypatch.setattr(media, "frame_size", lambda settings: (160, 90))
    output = tmp_path / "clips"
    output.mkdir()
    row = pipeline.render_clip(target, output, 1, local, shifted, settings, "synthetic")
    expected_duration = 2.0 if silence_cut else requested_end - requested_start
    assert row["media"]["duration"] == pytest.approx(expected_duration, abs=0.06)
    captions = pysubs2.load(str(output / "clip-001/captions.srt"))
    assert captions[0].start == (0 if silence_cut else 320)
    measured = quality.measure_output(output / "clip-001/video.mp4")
    assert measured["integrated_lufs"] == pytest.approx(-16.0, abs=0.5)
    if not silence_cut:
        # Before the first caption, compare the final picture with the exact
        # requested source frame, so matching durations alone cannot hide drift.
        def frame(path, start):
            return process(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(start),
                    "-i",
                    path,
                    "-frames:v",
                    "1",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "rawvideo",
                    "-",
                ]
            ).stdout

        reference = frame(target, offset)
        rendered = frame(output / "clip-001/video.mp4", 0)
        assert len(reference) == len(rendered) == 160 * 90 * 3
        assert sum(abs(a - b) for a, b in zip(reference, rendered)) / len(reference) < 3
