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
