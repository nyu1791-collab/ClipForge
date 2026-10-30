import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from clipforge import transcribe_pipeline as route
from clipforge.audio_understanding import Client
from clipforge.storage import PipelineError


def response():
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {
                            "audioTranscription": {
                                "speakerLabel": "spk:0",
                                "words": [
                                    {
                                        "word": "うん",
                                        "startOffset": "1.000s",
                                        "endOffset": "1.200s",
                                    }
                                ],
                            }
                        }
                    ]
                },
            }
        ]
    }


def client(tmp_path, monkeypatch, resume=None, continuation=False):
    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "test-token")
    output = tmp_path / ("output-" + str(len(list(tmp_path.iterdir()))))
    output.mkdir()
    return Client(output, resume, continuation)


def test_settings_and_request_are_the_same_and_saved(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        route, "encode_audio", lambda src, dst, *a, **kw: dst.write_bytes(b"audio")
    )
    sent = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(response()).encode()

    def fake(req, timeout):
        sent.append(json.loads(req.data))
        return Reply()

    monkeypatch.setattr(route.urllib.request, "urlopen", fake)
    clip = SimpleNamespace(start=0, end=20)
    result = route.request_chunk(c, "audio", clip, 1, 0, 0, 20, "sha")
    assert result == response()
    assert (
        sent[0]["generationConfig"]["audioTranscriptionConfig"]
        == route.transcription_config()
    )
    assert c.record["requests"][0]["identity"]["config"] == route.transcription_config()
    assert c.record["requests"][0]["status"] == "received"
    assert list(c.output.glob("transcribe-*.json"))


def test_resume_uses_saved_response_without_network(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        route, "encode_audio", lambda src, dst, *a, **kw: dst.write_bytes(b"audio")
    )

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(response()).encode()

    monkeypatch.setattr(route.urllib.request, "urlopen", lambda *a, **k: Reply())
    clip = SimpleNamespace(start=0, end=20)
    route.request_chunk(c, "audio", clip, 1, 0, 0, 20, "sha")
    c2 = client(tmp_path, monkeypatch, c.output, True)
    monkeypatch.setattr(
        route.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("unexpected model request"),
    )
    assert route.request_chunk(c2, "audio", clip, 1, 0, 0, 20, "sha") == response()
    assert c2.record["requests"][0]["reused"] is True


def test_nonretryable_http_failure_remains_nonretryable_on_resume(
    tmp_path, monkeypatch
):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        route, "encode_audio", lambda src, dst, *a, **kw: dst.write_bytes(b"audio")
    )

    def fail(*a, **k):
        raise HTTPError("https://example.invalid", 422, "error", {}, None)

    monkeypatch.setattr(route.urllib.request, "urlopen", fail)
    clip = SimpleNamespace(start=0, end=20)
    with pytest.raises(HTTPError):
        route.request_chunk(c, "audio", clip, 1, 0, 0, 20, "sha")
    c2 = client(tmp_path, monkeypatch, c.output, True)
    with pytest.raises(PipelineError, match="non-retryable"):
        route.request_chunk(c2, "audio", clip, 1, 0, 0, 20, "sha")


def test_transcribe_retries_rate_limit_after_server_delay(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(route, "encode_audio", lambda src, dst, *a, **kw: dst.write_bytes(b"audio"))
    monkeypatch.setattr(route.random, "uniform", lambda *args: 0)
    delays = []
    monkeypatch.setattr(route.time, "sleep", delays.append)
    calls = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(response()).encode()

    def request(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "60"}, None)
        return Reply()

    monkeypatch.setattr(route.urllib.request, "urlopen", request)
    route.request_chunk(c, "audio", SimpleNamespace(start=0, end=20), 1, 0, 0, 20, "sha")
    assert delays == [60]
    assert [r["status"] for r in c.record["requests"]] == ["failed", "received"]
    assert c.record["requests"][0]["retry_delay_seconds"] == 60


def test_transcribe_parallel_limit_is_explicit(monkeypatch):
    monkeypatch.delenv("CLIPFORGE_TRANSCRIBE_PARALLEL", raising=False)
    assert route.parallel_limit() == 1
    monkeypatch.setenv("CLIPFORGE_TRANSCRIBE_PARALLEL", "2")
    assert route.parallel_limit() == 2
    monkeypatch.setenv("CLIPFORGE_TRANSCRIBE_PARALLEL", "0")
    with pytest.raises(PipelineError):
        route.parallel_limit()


def test_long_topic_uses_overlap_and_chunk_local_speakers(monkeypatch):
    calls = []

    def fake(client, audio, clip, index, part, begin, length, sha):
        calls.append((part, begin, length))
        return response()

    monkeypatch.setattr(route, "request_chunk", fake)
    result = route.transcribe_clip(
        None, "audio", SimpleNamespace(start=0, end=1000), 1, "sha"
    )
    assert sorted(calls) == [(0, 0.0, 898.0), (1, 896.0, 104.0)]
    assert [s["start"] for s in result["segments"]] == [1.0, 897.0]


def test_six_topics_flow_through_transcription_and_cleanup(tmp_path, monkeypatch):
    import threading
    from clipforge import audio_understanding as editing
    from clipforge.models import SelectedClip

    barrier = threading.Barrier(6, timeout=5)
    calls = []

    def transcribe(c, audio, clip, index, sha):
        barrier.wait()
        calls.append(("transcribe", index))
        return {
            "language": "ja",
            "segments": [{"start": 1, "end": 2, "text": "うん"}],
            "untimed": [],
        }

    def clean(c, raw, index, clip, glossary, path, audio_source_key=None):
        calls.append(("cleanup", index))
        return {
            "transcript_access": "available",
            "segments": [{"start": 1, "end": 2, "text": "うん"}],
            "sound_effects": [],
            "finishing": {"model_requests": 1},
        }

    monkeypatch.setattr(route, "transcribe_clip", transcribe)
    monkeypatch.setattr(editing, "clean_selected_clip", clean)
    monkeypatch.setattr(
        editing, "encode_audio", lambda src, dst, *a, **kw: dst.write_bytes(b"audio")
    )
    monkeypatch.delenv("CLIPFORGE_STREAMING_RENDER", raising=False)
    c = client(tmp_path, monkeypatch)
    clips = [
        SelectedClip(
            start=i * 10,
            end=i * 10 + 10,
            candidate_id=str(i),
            title="話題",
            reason="会話",
            confidence=0.9,
        )
        for i in range(6)
    ]
    editing.process_selected(
        "audio",
        "https://example.invalid",
        c.output,
        None,
        None,
        False,
        {"audio_sha256": "sha"},
        c,
        clips,
        [],
    )
    assert sorted(i for phase, i in calls if phase == "transcribe") == list(range(1, 7))
    assert sorted(i for phase, i in calls if phase == "cleanup") == list(range(1, 7))
    output = json.loads((c.output / "selected-transcripts.json").read_text())
    assert (
        output["timestamp_source"] == "gemini_transcribe" and len(output["clips"]) == 6
    )


def test_unchanged_words_keep_original_times_but_edits_do_not_guess():
    from clipforge.audio_understanding import clean_selected_clip, selected_transcript
    from clipforge.models import SelectedClip

    clip = SelectedClip(
        start=0, end=10, candidate_id="one", title="話題", reason="会話", confidence=0.9
    )
    raw = {
        "segments": [
            {
                "start": 1.0,
                "end": 4.0,
                "text": "こんにちは",
                "words": [
                    {"start": 1.0, "end": 1.4, "text": "こん"},
                    {"start": 3.0, "end": 4.0, "text": "にちは"},
                ],
            }
        ]
    }

    class Fake:
        def __init__(self, text):
            self.text = text

        def generate(self, *args, **kwargs):
            return {
                "audio_access": "heard",
                "segments": [{"source_ids": [1], "text": self.text}],
                "sound_effects": [],
            }, {}

    unchanged = clean_selected_clip(Fake("こんにちは"), raw, 1, clip, [], "audio")
    result, _ = selected_transcript(unchanged, 10)
    assert [(word.text, word.start, word.end) for word in result.segments[0].words] == [
        ("こん", 1.0, 1.4),
        ("にちは", 3.0, 4.0),
    ]
    changed = clean_selected_clip(Fake("こんにちは。"), raw, 1, clip, [], "audio")
    result, _ = selected_transcript(changed, 10)
    assert [(word.text, word.start, word.end) for word in result.segments[0].words] == [
        ("こん", 1.0, 1.4),
        ("にちは。", 3.0, 4.0),
    ]
    assert result.segments[0].text == "こんにちは。"
    rewritten = clean_selected_clip(Fake("こんばんは。"), raw, 1, clip, [], "audio")
    result, _ = selected_transcript(rewritten, 10)
    assert result.segments[0].words == []


def test_gap_recovery_is_bounded_and_preserves_original_words(monkeypatch):
    clip = SimpleNamespace(start=100, end=500)
    calls = []

    def fake(client, audio, clip, index, part, begin, length, sha):
        calls.append((part, begin, length))
        value = response()
        word = value["candidates"][0]["content"]["parts"][0]["audioTranscription"][
            "words"
        ][0]
        word.update(startOffset="3s", endOffset="3.2s")
        return value

    monkeypatch.setattr(route, "request_chunk", fake)
    levels = {
        "status": "complete",
        "gaps": [
            {"start": 10, "end": 40, "peak_one_second_rms_dbfs": -15},
            {"start": 60, "end": 90, "peak_one_second_rms_dbfs": -20},
            {"start": 100, "end": 130, "peak_one_second_rms_dbfs": -18},
            {"start": 150, "end": 180, "peak_one_second_rms_dbfs": -70},
        ],
    }
    raw = {
        "segments": [
            {
                "start": 0,
                "end": 1,
                "text": "原文",
                "words": [{"start": 0, "end": 1, "text": "原文"}],
            }
        ],
        "untimed": [],
    }
    merged, report = route.recover_active_gaps(
        None, "audio", clip, 1, "sha", raw, levels
    )
    assert len(calls) == 2
    assert merged["segments"][0]["text"] == "原文"
    assert len(report["requests"]) == 2
    assert [row["text"] for row in merged["segments"]] == ["原文", "うん", "うん"]
    assert "not speech detection" in report["note"]


def test_gap_recovery_failure_does_not_erase_input(monkeypatch):
    def fail(*args):
        raise OSError("unavailable")

    monkeypatch.setattr(route, "request_chunk", fail)
    raw = {"segments": [{"start": 0, "end": 1, "text": "原文"}], "untimed": []}
    levels = {"gaps": [{"start": 10, "end": 40, "peak_one_second_rms_dbfs": -10}]}
    merged, report = route.recover_active_gaps(
        None, "audio", SimpleNamespace(start=0, end=60), 1, "sha", raw, levels
    )
    assert merged == raw
    assert report["requests"][0]["status"] == "unavailable"


def test_overlap_removes_only_high_confidence_repeated_words(monkeypatch):
    def fake(client, audio, clip, index, part, begin, length, sha):
        value = response()
        word = value["candidates"][0]["content"]["parts"][0]["audioTranscription"][
            "words"
        ][0]
        word.update(
            word="こんにちは",
            startOffset="896.2s" if part == 0 else "0.4s",
            endOffset="897.6s" if part == 0 else "1.9s",
        )
        return value

    monkeypatch.setattr(route, "request_chunk", fake)
    result = route.transcribe_clip(
        None, "audio", SimpleNamespace(start=0, end=1000), 1, "sha"
    )
    assert [row["text"] for row in result["segments"]] == ["こんにちは"]


def test_only_explicit_nonspeech_recovery_can_be_omitted():
    from clipforge.audio_understanding import clean_selected_clip
    from clipforge.models import SelectedClip

    clip = SelectedClip(
        start=0, end=20, candidate_id="one", title="話題", reason="会話", confidence=0.9
    )
    raw = {
        "segments": [
            {"start": 1, "end": 2, "text": "普通の発言"},
            {"start": 3, "end": 4, "text": "誤認候補", "gap_recovery": True},
        ]
    }

    class Fake:
        def generate(self, *args, **kwargs):
            return {
                "audio_access": "heard",
                "segments": [
                    {"source_ids": [1], "text": "", "omit_non_speech": True},
                    {"source_ids": [2], "text": "", "omit_non_speech": True},
                ],
                "sound_effects": [],
            }, {}

    result = clean_selected_clip(Fake(), raw, 1, clip, [], "audio")
    assert [row["text"] for row in result["segments"]] == ["普通の発言"]
