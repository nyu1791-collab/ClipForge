import pysubs2
import pytest

from clipforge import captions, signals
from clipforge.models import Segment, SelectedClip, Transcript, Word
from clipforge.storage import PipelineError


def test_chat_filtering_offset_reactions_and_privacy(settings):
    settings.chat_offset_seconds = -2
    messages = [
        {"time_in_seconds": 5, "message": "草", "author": "private-user"},
        {"time": 8, "message": "ordinary"},
        {"time": "nan", "message": "lol"},
        {"time": -9},
        {"time": 100},
        None,
        {"time": "bad"},
    ]
    result = signals.chat_signals(30, settings, messages)
    assert result["status"] == "provided"
    assert result["events"] == [
        {"time": 3.0, "weight": 2.0},
        {"time": 6.0, "weight": 1.0},
    ]
    assert "private-user" not in str(result)
    assert "ordinary" not in str(result)


@pytest.mark.parametrize("supplied", [{}, "text", [None] * 100001])
def test_chat_rejects_invalid_supplied_data(settings, supplied):
    with pytest.raises(PipelineError):
        signals.chat_signals(30, settings, supplied)


def test_optional_chat_failure_is_explicit(settings, monkeypatch):
    import chat_downloader

    class Broken:
        def get_chat(self, *args, **kwargs):
            assert kwargs["max_attempts"] == 1
            raise RuntimeError("private provider body")

        def close(self):
            pass

    monkeypatch.setattr(chat_downloader, "ChatDownloader", Broken)
    settings.chat_url = "https://www.youtube.com/watch?v=fixture"
    assert signals.chat_signals(30, settings)["status"] == "unavailable_RuntimeError"


@pytest.mark.parametrize("duration", [3, 5, 10, 30, 3600])
def test_candidates_stay_in_bounds_and_avoid_duplicates(settings, transcript, duration):
    result = signals.candidates(
        {"duration": duration},
        transcript,
        {"boundaries": [5, 10, 15]},
        {"events": []},
        settings,
    )
    assert 1 <= len(result) <= settings.candidates
    assert len({r["id"] for r in result}) == len(result)
    for index, item in enumerate(result):
        assert 0 <= item["start"] < item["end"] <= duration
        assert (
            settings.min_seconds <= item["end"] - item["start"] <= settings.max_seconds
        )
        for previous in result[:index]:
            overlap = max(
                0,
                min(item["end"], previous["end"])
                - max(item["start"], previous["start"]),
            )
            assert (
                overlap
                / min(item["end"] - item["start"], previous["end"] - previous["start"])
                <= 0.45
            )


def test_temporal_diversity_and_chat_peaks(settings):
    empty = Transcript(language="und", segments=[])
    info = {"duration": 600}
    scenes = {"boundaries": []}
    result = signals.candidates(info, empty, scenes, {"events": []}, settings)
    assert any(r["start"] > 300 for r in result)
    chat = {"events": [{"time": 480 + i / 10, "weight": 2} for i in range(30)]}
    result = signals.candidates(info, empty, scenes, chat, settings)
    assert result[0]["start"] <= 480 < result[0]["end"]


def test_short_video_rejected(settings, transcript):
    with pytest.raises(PipelineError):
        signals.candidates(
            {"duration": 1}, transcript, {"boundaries": []}, {"events": []}, settings
        )


def test_no_audio_skips_model_loading(settings):
    assert signals.transcribe(None, {"audio": False}, settings) == {
        "language": "und",
        "segments": [],
    }


@pytest.mark.parametrize("aspect", ["9:16", "16:9", "1:1"])
@pytest.mark.parametrize("height", [720, 1080])
@pytest.mark.parametrize("kind", ["plain", "karaoke"])
def test_subtitle_time_layout_escaping_and_formats(
    tmp_path, settings, aspect, height, kind
):
    settings.aspect, settings.height, settings.caption = aspect, height, kind
    text = (
        "これは日本語の長い字幕を画面の中に収めるための品質確認です。" * 2
        + r"{\pos(0,0)}"
    )
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(
                start=8, end=18, text=text, words=[Word(start=8, end=18, text=text)]
            )
        ],
    )
    clip = SelectedClip(
        start=10, end=16, candidate_id="manual", title="t", reason="r", confidence=1
    )
    result = captions.create(transcript, clip, tmp_path, settings)
    ass = pysubs2.load(str(tmp_path / "captions.ass"))
    srt = pysubs2.load(str(tmp_path / "captions.srt"))
    assert result["events"] == len(ass) > 0
    assert len(srt) == len(ass)
    assert all(0 <= e.start < e.end <= 6000 for e in ass)
    assert all(a.end <= b.start for a, b in zip(ass, ass[1:]))
    assert all("\\pos(" not in e.text for e in ass)
    assert all("\\kf" not in e.text for e in srt)
    assert ("\\kf" in (tmp_path / "captions.ass").read_text()) == (kind == "karaoke")
    line_cells = max(
        12,
        min(52, int((int(ass.info["PlayResX"]) - 70) / (round(height * 0.048) * 0.6))),
    )
    for event in ass:
        lines = event.plaintext.splitlines()
        assert len(lines) <= 2
        assert all(captions.width(line) <= line_cells for line in lines)


def test_empty_captions_valid(tmp_path, settings):
    clip = SelectedClip(
        start=0, end=5, candidate_id="manual", title="t", reason="r", confidence=1
    )
    result = captions.create(
        Transcript(language="und", segments=[]), clip, tmp_path, settings
    )
    assert result["events"] == 0
    assert (tmp_path / "captions.ass").is_file()
    assert (tmp_path / "captions.srt").read_text() == ""
