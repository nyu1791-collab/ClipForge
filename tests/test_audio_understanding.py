import json
from types import SimpleNamespace

import pytest

from clipforge import audio_understanding as route, storage
from clipforge import transcribe_pipeline
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
        lambda source, target, *args, **kwargs: target.write_bytes(b"synthetic opus"),
    )
    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "synthetic")
    monkeypatch.delenv("CLIPFORGE_STREAMING_RENDER", raising=False)
    monkeypatch.setattr(
        storage,
        "process",
        lambda cmd, **kw: __import__("pathlib")
        .Path(cmd[-1])
        .write_bytes(b"synthetic references"),
    )
    calls = []

    def generate(
        self, phase, prompt, audio=None, research=False, audio_source_key=None
    ):
        calls.append(
            (
                phase,
                prompt,
                research,
                audio.read_bytes() if audio else None,
                audio_source_key,
            )
        )
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
            "segments": [
                {
                    "source_ids": [1],
                    "text": "ああ、そうか。これは省略せず全部字幕に残します。",
                }
            ],
            "sound_effects": [],
        }, {}

    def selected_transcribe(client, audio, clip, index, sha):
        return {
            "segments": [
                {
                    "start": 0.5,
                    "end": 3.0,
                    "text": "ああ、そうか。これは省略せず全部字幕に残します。",
                }
            ]
        }

    monkeypatch.setattr(route.Client, "generate", generate)
    monkeypatch.setattr(transcribe_pipeline, "transcribe_clip", selected_transcribe)
    output = tmp_path / "review"
    route.plan(audio, URL, output, Settings(aspect="16:9", height=360))
    return output, audio, calls


def test_audio_plan_uses_only_selected_transcribe_results(planned):
    output, audio, calls = planned
    assert calls[0][0] == "topics-0"
    assert all(call[0] != "terminology" for call in calls)
    assert sorted(p for p, _, _, _, _ in calls if p.startswith("transcript-")) == [
        f"transcript-{i}" for i in range(1, 6)
    ]
    assert all(
        "話題区間だけを返します" in prompt
        for p, prompt, _, _, _ in calls
        if p.startswith("topics")
    )
    assert all(
        "要約もしない" in prompt and attached == b"synthetic opus" and audio_key
        for p, prompt, _, attached, audio_key in calls
        if p.startswith("transcript")
    )
    assert not (audio.parent / "asr-receipt.json").exists()
    receipt = json.loads((output / "audio-editor-receipt.json").read_text())
    assert receipt["status"] == "complete" and receipt["full_source_asr"] is False
    selected = json.loads((output / "selected-transcripts.json").read_text())
    assert (
        len(selected["clips"]) == 5
        and selected["timestamp_source"] == "gemini_transcribe"
    )
    assert all("ああ、そうか" in c["segments"][0]["text"] for c in selected["clips"])
    assert not (output / "focused-2.ogg").exists()


@pytest.fixture
def test_terminology_is_recorded_as_skipped_and_selected_topics_still_finish(planned):
    output, _, _ = planned
    glossary = json.loads((output / "terminology-glossary.json").read_text())
    assert glossary["terms"] == []
    assert glossary["diagnostics"] == {"status": "skipped", "reason": "not_in_route"}
    selected = json.loads((output / "selected-transcripts.json").read_text())
    assert len(selected["clips"]) == 5


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
def test_estimated_timing_is_made_renderable_without_dropping_text(segments):
    transcript, _ = route.selected_transcript(
        {"audio_access": "heard", "complete": True, "segments": segments}, 10
    )
    assert all(0 <= row.start < row.end <= 10 for row in transcript.segments)
    assert "\n".join(row.text for row in transcript.segments) == "\n".join(
        row["text"] for row in segments
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
    assert drawn[0][2]["speaker_turns"][0]["speaker_id"] is None


def test_failed_antigravity_call_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise route.AntigravityError(
            "Antigravity request failed: PermissionError", retryable=False
        )

    monkeypatch.setattr(route, "generate_text", fail)
    client = route.Client(tmp_path)
    with pytest.raises(route.AntigravityError):
        client.generate("topics-0", "audio topics")
    assert len(calls) == 1
    assert client.record["requests"][0]["status"] == "failed"
    assert client.record["requests"][0]["error_class"] == "non_retryable"


def test_transient_antigravity_call_is_retried_up_to_the_limit(tmp_path, monkeypatch):
    calls = []

    def busy(*args, **kwargs):
        calls.append(1)
        raise route.AntigravityError(
            "Antigravity request failed: RuntimeError", retryable=True
        )

    monkeypatch.setattr(route, "generate_text", busy)
    monkeypatch.setattr(route.time, "sleep", lambda seconds: None)
    client = route.Client(tmp_path)
    with pytest.raises(route.AntigravityError):
        client.generate("topics-0", "audio topics")
    assert len(calls) == route.MAX_MODEL_ATTEMPTS


def test_subtitle_cleanup_and_topics_use_high_thinking(tmp_path, monkeypatch):
    """The actual request keeps the other stages on their own profiles."""
    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "synthetic")
    client = route.Client(tmp_path)
    sent = []

    def capture(
        phase,
        model,
        thinking,
        files,
        sources,
        rules,
        prompt_path,
        prompt_hash,
        audio,
        audio_key,
        parts,
        research,
        attempt,
    ):
        sent.append((model, thinking, research))
        return {}, {}

    monkeypatch.setattr(client, "_generate_attempt", capture)
    client.generate("transcript-1", "字幕の整文")
    client.generate("topics-0", "話題の選定")
    client.generate("terminology", "用語の調査", research=True)
    assert sent == [
        ("gemini-3.8-flash", "HIGH", False),
        ("gemini-3.8-flash", "HIGH", False),
        ("gemini-3.5-flash", "MINIMAL", True),
    ]


def test_overview_agent_count_follows_recording_length():
    assert [w["index"] for w in route.overview_windows(3600)] == [0]
    assert len(route.overview_windows(3601)) == 2
    assert len(route.overview_windows(7200)) == 2
    assert len(route.overview_windows(7201)) == 4
    assert len(route.overview_windows(18094)) == 4
    windows = route.overview_windows(21600)
    assert [(w["owner_start"], w["owner_end"]) for w in windows] == [
        (0, 5400),
        (5400, 10800),
        (10800, 16200),
        (16200, 21600),
    ]
    assert windows[1]["audio_start"] == 4800 and windows[1]["audio_end"] == 11400


def _topic(start, end):
    return route.SelectedClip(
        start=start, end=end, candidate_id="t", title="t", reason="r", confidence=0.9
    )


def test_cut_moves_to_a_topic_end_and_each_topic_comes_from_its_owner():
    candidates = [
        (0, _topic(3000, 3400)),
        (0, _topic(3450, 3700)),
        (1, _topic(3460, 3700)),
        (1, _topic(3800, 4000)),
    ]
    kept, cuts = route.owned_topics(candidates, route.overview_windows(7200))
    assert cuts == [3700]
    assert [(t.start, t.end) for t in kept] == [
        (3000, 3400),
        (3450, 3700),
        (3800, 4000),
    ]


def test_cut_stays_even_when_no_topic_ends_near_it():
    kept, cuts = route.owned_topics(
        [(0, _topic(100, 200))], route.overview_windows(7200)
    )
    assert cuts == [3600]
    assert [(t.start, t.end) for t in kept] == [(100, 200)]
