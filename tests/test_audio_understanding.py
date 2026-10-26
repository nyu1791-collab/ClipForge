import json
from types import SimpleNamespace
import urllib.error

import pytest

from clipforge import audio_understanding as route, storage
from clipforge.models import Settings
from clipforge.storage import PipelineError

URL = "https://kick.com/example/videos/01a171d4-2920-7e7b-a842-e2b580698688"


@pytest.fixture
def planned(tmp_path, monkeypatch):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"synthetic input")
    monkeypatch.setattr(
        route.audio_first, "audio_info", lambda path: {"duration": 1800}
    )
    monkeypatch.setattr(
        route.audio_first, "allocate_audio", lambda *a: pytest.fail("full ASR called")
    )
    monkeypatch.setattr(
        route,
        "encode_audio",
        lambda source, target, *args: target.write_bytes(b"synthetic opus"),
    )
    monkeypatch.setattr(
        storage,
        "process",
        lambda cmd, **kw: __import__("pathlib")
        .Path(cmd[-1])
        .write_bytes(b"synthetic references"),
    )
    calls = []

    def generate(self, phase, prompt, audio=None, research=False):
        calls.append((phase, prompt, research))
        if phase.startswith("topics"):
            return {
                "audio_access": "heard",
                "topics": [
                    {
                        "start": i * 330,
                        "end": i * 330 + 300,
                        "title": f"話題{i}",
                        "reason": "会話の文脈",
                        "confidence": 0.9,
                    }
                    for i in range(5)
                ],
            }, {}
        if research:
            return {"terms": []}, {}
        return {
            "audio_access": "heard",
            "complete": True,
            "segments": [
                {
                    "start": 0.5,
                    "end": 3,
                    "text": "ああ、そうか。これは省略せず全部字幕に残します。",
                    "speaker_id": "speaker_2",
                    "confidence": 0.95,
                }
            ],
        }, {}

    monkeypatch.setattr(route.Client, "generate", generate)
    output = tmp_path / "review"
    route.plan(audio, URL, output, Settings(aspect="16:9", height=360))
    return output, audio, calls


def test_audio_only_plan_never_requires_an_asr_transcript(planned):
    output, audio, calls = planned
    assert [p for p, _, _ in calls] == [
        "topics-0",
        "terminology",
        *[f"transcript-{i}" for i in range(1, 6)],
    ]
    assert all(
        "全文文字起こしは不要" in prompt
        for p, prompt, _ in calls
        if p.startswith("topics")
    )
    assert all(
        "文字数の圧縮は禁止" in prompt
        for p, prompt, _ in calls
        if p.startswith("transcript")
    )
    assert not (audio.parent / "asr-receipt.json").exists()
    receipt = json.loads((output / "audio-editor-receipt.json").read_text())
    assert receipt["status"] == "complete" and receipt["full_source_asr"] is False
    selected = json.loads((output / "selected-transcripts.json").read_text())
    assert (
        len(selected["clips"]) == 5
        and selected["timestamp_source"] == "gemini_estimated"
    )
    assert all("ああ、そうか" in c["segments"][0]["text"] for c in selected["clips"])
    # Later clips include a short voice reference, rather than inferring identity from words.
    assert (output / "focused-2.ogg").is_file()


def test_resume_cannot_apply_responses_to_another_audio(planned, tmp_path, monkeypatch):
    output, audio, _ = planned
    audio.write_bytes(b"different audio")
    with pytest.raises(PipelineError, match="another input"):
        route.plan(
            audio, URL, tmp_path / "replay", Settings(aspect="16:9", height=360), output
        )


@pytest.mark.parametrize(
    "segments",
    [
        [],
        [{"start": 0, "end": 11, "text": "x"}],
        [{"start": 0, "end": 2, "text": "x"}, {"start": 1, "end": 3, "text": "y"}],
    ],
)
def test_invalid_estimated_timing_is_rejected(segments):
    with pytest.raises(PipelineError):
        route.selected_transcript(
            {"audio_access": "heard", "complete": True, "segments": segments}, 10
        )


def test_unavailable_or_incomplete_audio_is_not_renderable():
    with pytest.raises(PipelineError):
        route.selected_transcript({"audio_access": "unavailable", "complete": True}, 10)


def test_renderer_keeps_remux_offset_out_of_speaker_times(
    planned, tmp_path, monkeypatch
):
    output, _, _ = planned
    monkeypatch.setattr(
        route, "open_source", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )

    def retrieve(vod, target, *a, **kw):
        target.write_bytes(b"video")
        return {"rendition_height": 360, "start_offset": 2}

    monkeypatch.setattr(route.audio_first, "retrieved", retrieve)
    drawn = []

    def draw(source, root, index, clip, transcript, settings, key, **kwargs):
        drawn.append((clip, transcript, kwargs))
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(route.pipeline, "render_clip", draw)
    route.render(
        output / "final-selection.json",
        output / "selected-transcripts.json",
        tmp_path / "delivery",
        Settings(aspect="16:9", height=360),
    )
    assert len(drawn) == 5
    assert drawn[0][1].segments[0].start == 2.5
    assert drawn[0][2]["speaker_turns"][0]["start"] == 0.5


def test_failed_vertex_call_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "synthetic")
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError(
            "https://example.test", 400, "unsupported", {}, None
        )

    monkeypatch.setattr(route.urllib.request, "urlopen", fail)
    client = route.Client(tmp_path)
    with pytest.raises(urllib.error.HTTPError):
        client.generate("topics-0", "audio topics")
    assert len(calls) == 1
    assert client.record["requests"][0]["status"] == "failed"
