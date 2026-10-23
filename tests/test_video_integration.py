import os
from pathlib import Path
import subprocess

import pytest
import pysubs2

from clipforge import media, pipeline, signals
from clipforge.models import (
    Candidate,
    Segment,
    SelectedClip,
    Transcript,
    Word,
)
from clipforge.storage import digest, write_json


@pytest.fixture(scope="module")
def source_video():
    value = os.environ.get("CLIPFORGE_FIXTURE_PATH")
    if not value:
        pytest.skip("Synthetic video fixture is generated on the GitHub runner")
    path = Path(value)
    assert path.is_file()
    return path


@pytest.fixture(autouse=True)
def fixture_render_size(settings):
    # Integration pixel checks explicitly target 720p; production defaults may be taller.
    settings.height = 720


@pytest.mark.integration
@pytest.mark.parametrize("aspect", ["9:16", "16:9", "1:1"])
@pytest.mark.parametrize("framing", ["fit", "center-crop"])
def test_real_cut_subtitles_frames_audio_and_resume(
    source_video, tmp_path, settings, aspect, framing
):
    settings.aspect, settings.framing = aspect, framing
    text = "この日本語字幕は切り抜いた動画の中で正しく表示されます。"
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(
                start=13, end=19, text=text, words=[Word(start=13, end=19, text=text)]
            )
        ],
    )
    reference = tmp_path / "transcript.json"
    write_json(reference, transcript.model_dump())
    root = Path(os.environ.get("CLIPFORGE_QA_OUTPUT", str(tmp_path)))
    output = root / (aspect.replace(":", "x") + "-" + framing)
    before = digest(source_video)
    result = pipeline.cut(source_video, output, 13, 19, settings, reference)
    target = output / "clips/clip-001/video.mp4"
    info = media.probe(target)
    assert result["status"] == "complete"
    assert abs(info["duration"] - 6) < 0.15
    assert info["audio"]
    assert info["height"] == 720
    subs = pysubs2.load(str(target.parent / "captions.ass"))
    assert len(subs) > 0
    assert all(0 <= s.start < s.end <= 6000 for s in subs)
    # At 13 seconds the source is red. Check an actual decoded center pixel.
    pixel = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(target),
            "-frames:v",
            "1",
            "-vf",
            "crop=2:2:iw/2:ih/2,scale=1:1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    assert len(pixel) == 3 and pixel[0] > 150 and pixel[1] < 90 and pixel[2] < 90
    assert digest(source_video) == before
    import cv2

    capture = cv2.VideoCapture(str(target))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, 2000)
        ok, frame = capture.read()
        assert ok
        assert cv2.imwrite(
            str(
                root / ("preview-" + aspect.replace(":", "x") + "-" + framing + ".png")
            ),
            frame,
        )
    finally:
        capture.release()
    assert (
        pipeline.cut(source_video, output, 13, 19, settings, reference, resume=True)
        == result
    )


@pytest.mark.integration
def test_scene_detection_finds_known_boundaries(source_video):
    result = signals.scene_boundaries(source_video, 36)
    for expected in [12, 24]:
        assert any(abs(t - expected) < 0.3 for t in result["boundaries"])


@pytest.mark.integration
def test_preview_preserves_original_interval(source_video, tmp_path, settings):
    candidate = Candidate(
        id="candidate-001",
        start=12.3,
        end=20.3,
        heuristic_score=1,
        speech_seconds=8,
        chat_reactions=0,
        scene_boundaries=0,
    )
    info = media.preview(source_video, tmp_path / "preview.mp4", candidate, settings)
    assert abs(info["duration"] - 8) < 0.3
    assert info["audio"]
    assert (info["width"], info["height"]) == (640, 360)


@pytest.mark.integration
def test_silent_video_render_has_no_audio(source_video, tmp_path, settings):
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-i",
            str(source_video),
            "-an",
            "-c:v",
            "copy",
            str(silent),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = pipeline.cut(silent, tmp_path / "result", 13, 19, settings)
    assert result["status"] == "complete"
    assert not media.probe(tmp_path / "result/clips/clip-001/video.mp4")["audio"]


@pytest.mark.asr
def test_real_cpu_asr_and_manual_pipeline(source_video, tmp_path, settings):
    settings.whisper_model = "tiny.en"
    settings.whisper_revision = "0d3d19a32d3338f10357c0889762bd8d64bbdeba"
    settings.language = "en"
    info = media.probe(source_video)
    transcript = Transcript.model_validate(
        signals.transcribe(source_video, info, settings)
    )
    assert transcript.segments
    assert any(s.words for s in transcript.segments)
    text = " ".join(s.text.lower() for s in transcript.segments)
    assert any(word in text for word in ["screen", "video", "important", "captions"])
    assert all(0 <= s.start < s.end <= info["duration"] for s in transcript.segments)
    assert all(
        s.start <= w.start < w.end <= s.end
        for s in transcript.segments
        for w in s.words
    )
    choice = tmp_path / "manual.json"
    write_json(
        choice,
        {
            "clips": [
                SelectedClip(
                    start=13,
                    end=21,
                    candidate_id="manual",
                    title="Synthetic red scene",
                    reason="Known scene boundary",
                    confidence=1,
                ).model_dump()
            ]
        },
    )
    result = pipeline.run(
        source_video, tmp_path / "full-pipeline", settings, selection_file=choice
    )
    assert result["status"] == "complete"
    assert result["selection_receipt"]["generation_requests"] == 0
    assert result["rendered"][0]["captions"]["events"] > 0


@pytest.mark.asr
def test_default_model_recognizes_japanese(source_video, settings):
    japanese = source_video.with_name("source-japanese.mp4")
    assert japanese.is_file(), "Generate the fixture with --japanese"
    settings.language = "ja"
    # The default model, at the revision that is pinned for it.
    assert (
        signals.model_revision(settings)
        == signals.PINNED_REVISIONS[settings.whisper_model]
    )
    info = media.probe(japanese)
    transcript = Transcript.model_validate(signals.transcribe(japanese, info, settings))
    text = "".join(s.text for s in transcript.segments)
    assert sum(word in text for word in ["字幕", "画面", "確認", "音声"]) >= 2
    assert any(s.words for s in transcript.segments)
    assert all(0 <= s.start < s.end <= info["duration"] for s in transcript.segments)
    assert all(
        s.start <= w.start < w.end <= s.end
        for s in transcript.segments
        for w in s.words
    )
