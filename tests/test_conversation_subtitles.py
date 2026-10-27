"""Subtitle preservation and display checks with synthetic data only."""

import json
from types import SimpleNamespace

import pysubs2
import pytest

from clipforge import asr, audio_understanding, captions, storage
from clipforge.models import Segment, Settings, Transcript


def test_asr_keeps_characters_with_collapsed_timestamps():
    rows = asr.segments(
        [
            {"char": "聞", "start": 0.0, "end": 0.1},
            {"char": "こ", "start": 0.01, "end": 0.02},
            {"char": "え", "start": 0.1, "end": 0.2},
        ],
        0.0,
        1.0,
    )
    assert "".join(row["text"] for row in rows) == "聞こえ"
    assert "".join(word["text"] for row in rows for word in row["words"]) == "聞こえ"


def test_empty_model_reply_cannot_delete_meaningful_sentence():
    class Client:
        def generate(self, *args, **kwargs):
            return {
                "segments": [
                    {"source_ids": [1], "text": "", "omit_filler": True},
                    {"source_ids": [2], "text": "", "omit_filler": True},
                    {"source_ids": [3], "text": "", "omit_filler": True},
                ]
            }, {}

    raw = {
        "segments": [
            {"start": 0, "end": 1, "text": "それは違います。"},
            {"start": 1, "end": 2, "text": "えー"},
            {"start": 2, "end": 2.5, "text": "あの"},
        ]
    }
    clip = SimpleNamespace(start=0, end=3, title="話題", reason="文脈")
    result = audio_understanding.clean_selected_clip(Client(), raw, 1, clip, [])
    assert [row["text"] for row in result["segments"]] == ["それは違います。", "あの"]


def test_same_audio_cleanup_request_assigns_voices_without_merging_speakers():
    class Client:
        def __init__(self):
            self.calls = []

        def generate(self, phase, prompt, audio=None):
            self.calls.append((phase, audio))
            return {
                "audio_access": "heard",
                "speaker_assignments": [
                    {"source_id": 1, "speaker_id": "speaker_1", "confidence": 0.95},
                    {"source_id": 2, "speaker_id": "speaker_2", "confidence": 0.9},
                    {"source_id": 3, "speaker_id": "speaker_1", "confidence": 0.95},
                    {"source_id": 3, "speaker_id": "speaker_2", "confidence": 0.95},
                ],
                "segments": [{"source_ids": [1, 2], "text": "勝手に一人へまとめた文"}],
            }, {}

    raw = {
        "segments": [
            {"start": i, "end": i + 0.9, "text": f"元の発言{i}"} for i in range(3)
        ]
    }
    client = Client()
    clip = SimpleNamespace(start=0, end=4, title="話題", reason="文脈")
    result = audio_understanding.clean_selected_clip(
        client, raw, 1, clip, [], "audio.ogg"
    )
    assert client.calls == [("transcript-1", "audio.ogg")]
    assert [row["text"] for row in result["segments"]] == [
        f"元の発言{i}" for i in range(3)
    ]
    assert [row["speaker_id"] for row in result["segments"]] == [
        "speaker_1",
        "speaker_2",
        "unknown",
    ]
    transcript, turns = audio_understanding.selected_transcript(result, 4)
    assert len(transcript.segments) == 3
    assert [turn["speaker_id"] for turn in turns] == ["speaker_1", "speaker_2", None]


def test_speaker_assignment_requires_heard_audio():
    class Client:
        def generate(self, *args, **kwargs):
            return {
                "speaker_assignments": [
                    {"source_id": 1, "speaker_id": "speaker_1", "confidence": 1.0}
                ]
            }, {}

    raw = {"segments": [{"start": 0, "end": 1, "text": "内容"}]}
    clip = SimpleNamespace(start=0, end=2, title="話題", reason="文脈")
    result = audio_understanding.clean_selected_clip(
        Client(), raw, 1, clip, [], "audio.ogg"
    )
    assert result["segments"][0]["speaker_id"] == "unknown"


def test_changed_speaker_prompt_never_resends_previously_attempted_cleanup(
    tmp_path, monkeypatch
):
    saved = tmp_path / "saved"
    output = tmp_path / "new"
    saved.mkdir()
    output.mkdir()
    (saved / "audio-editor-receipt.json").write_text(
        json.dumps({"requests": [{"phase": "transcript-1-old", "status": "received"}]})
    )
    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "synthetic")
    monkeypatch.setattr(
        audio_understanding.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("model request forbidden"),
    )
    client = audio_understanding.Client(output, resume=saved, continue_missing=True)
    with pytest.raises(storage.PipelineError, match="No complete saved response"):
        client.generate("transcript-1", "new prompt", audio=tmp_path / "audio.ogg")
    assert client.record["new_generation_requests"] == 0


@pytest.mark.parametrize("lifted", [False, True])
def test_two_known_speakers_get_distinct_colors_and_positions(
    tmp_path, monkeypatch, lifted
):
    class Measure:
        def width(self, text):
            return len(text) * 10

    monkeypatch.setattr(
        captions,
        "_caption_layout",
        lambda settings: (18, "synthetic", Measure(), 500, True),
    )
    monkeypatch.setattr(captions.fonts, "missing_glyphs", lambda *args: [])
    settings = Settings(
        aspect="16:9", height=360, caption="plain", caption_preserve_text=True
    )
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=0, end=2, text="一人目です"),
            Segment(start=3, end=5, text="二人目です"),
        ],
    )
    turns = [
        {"start": 0, "end": 2, "speaker_id": "speaker_1", "confidence": 0.95},
        {"start": 3, "end": 5, "speaker_id": "speaker_2", "confidence": 0.95},
    ]
    captions.create(
        transcript,
        SimpleNamespace(start=0, end=6, title=None),
        tmp_path,
        settings,
        lifts=[0.2, 0.2] if lifted else None,
        speaker_turns=turns,
    )
    ass = pysubs2.load(str(tmp_path / "captions.ass"))
    assert ass.styles["speaker_1"].alignment == pysubs2.Alignment.BOTTOM_LEFT
    assert ass.styles["speaker_2"].alignment == pysubs2.Alignment.BOTTOM_RIGHT
    assert ass.styles["speaker_1"].primarycolor != ass.styles["speaker_2"].primarycolor
    assert [event.style for event in ass] == ["speaker_1", "speaker_2"]
    if lifted:
        assert r"{\an1}" in ass[0].text and r"{\an3}" in ass[1].text
