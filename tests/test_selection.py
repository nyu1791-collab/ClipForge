import json
from types import SimpleNamespace

import pytest
from google import genai
from google.genai import types

from clipforge import selection
from clipforge.models import Candidate, Transcript
from clipforge.storage import PipelineError


@pytest.fixture
def selection_fixture(tmp_path, monkeypatch):
    candidates = [
        Candidate(
            id="candidate-001",
            start=20,
            end=30,
            heuristic_score=1,
            speech_seconds=8,
            chat_reactions=0,
            scene_boundaries=1,
        )
    ]
    path = tmp_path / "preview.mp4"
    path.write_bytes(b"synthetic-preview")
    payload = {
        "clips": [
            {
                "candidate_id": "candidate-001",
                "start": 1,
                "end": 7,
                "title": "見どころ",
                "reason": "映像と音声の根拠",
                "confidence": 0.8,
            }
        ]
    }
    events = {"uploads": [], "deletes": [], "requests": [], "closed": False}
    control = {
        "payload": payload,
        "finish": types.FinishReason.STOP,
        "fail_generate": False,
        "fail_delete": False,
        "file_state": types.FileState.ACTIVE,
    }

    class Files:
        def upload(self, **kwargs):
            events["uploads"].append(kwargs)
            return types.File(
                name="files/synthetic",
                uri="https://example.invalid/fixture",
                mime_type="video/mp4",
                state=control["file_state"],
            )

        def delete(self, **kwargs):
            events["deletes"].append(kwargs)
            if control["fail_delete"]:
                raise RuntimeError("private-error")

        def get(self, **kwargs):
            return types.File(name="files/synthetic", state=types.FileState.FAILED)

    class Models:
        def generate_content(self, **kwargs):
            events["requests"].append(kwargs)
            if control["fail_generate"]:
                raise RuntimeError("provider body with private values")
            return types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        finish_reason=control["finish"],
                        content=types.Content(
                            parts=[types.Part(text=json.dumps(control["payload"]))]
                        ),
                    )
                ],
                model_version="test-video-model",
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=10
                ),
            )

    def client(**kwargs):
        assert kwargs["http_options"].retry_options.attempts == 1
        return SimpleNamespace(
            files=Files(), models=Models(), close=lambda: events.update(closed=True)
        )

    monkeypatch.setattr(genai, "Client", client)
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-value")
    monkeypatch.setattr(selection.time, "sleep", lambda seconds: None)
    return candidates, [path], control, events


def call(settings, fixture):
    candidates, paths, _, _ = fixture
    return selection.choose(
        None,
        paths,
        candidates,
        Transcript(language="ja", segments=[]),
        {"duration": 90},
        settings,
    )


def test_video_parts_exact_model_and_global_timestamps(settings, selection_fixture):
    result = call(settings, selection_fixture)
    _, _, _, events = selection_fixture
    assert result["clips"][0]["start"] == 21
    assert result["clips"][0]["end"] == 27
    assert result["receipt"]["generation_requests"] == 1
    assert result["receipt"]["remote_file_cleanup"] == "complete"
    request = events["requests"][0]
    assert request["model"] == settings.gemini_model
    assert any(
        p.file_data
        and p.file_data.mime_type == "video/mp4"
        and p.video_metadata.fps == 2
        for p in request["contents"]
    )
    assert len(events["deletes"]) == 1 and events["closed"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"candidate_id": "unknown"},
        {"start": -1},
        {"end": 12},
        {"start": 1, "end": 2},
        {"confidence": 2},
        {"start": float("nan")},
        {"title": ""},
    ],
)
def test_invalid_response_stops_without_retry(settings, selection_fixture, invalid):
    _, _, control, events = selection_fixture
    control["payload"]["clips"][0].update(invalid)
    with pytest.raises(PipelineError):
        call(settings, selection_fixture)
    assert len(events["requests"]) == 1
    assert len(events["deletes"]) == 1
    assert events["closed"]


@pytest.mark.parametrize(
    "finish", [types.FinishReason.MAX_TOKENS, types.FinishReason.SAFETY]
)
def test_nonfinal_responses_rejected(settings, selection_fixture, finish):
    _, _, control, events = selection_fixture
    control["finish"] = finish
    with pytest.raises(PipelineError):
        call(settings, selection_fixture)
    assert len(events["requests"]) == 1 and len(events["deletes"]) == 1


def test_exception_redacted_and_files_deleted(settings, selection_fixture, capsys):
    _, _, control, events = selection_fixture
    control["fail_generate"] = True
    with pytest.raises(PipelineError) as caught:
        call(settings, selection_fixture)
    assert "private values" not in str(caught.value)
    assert len(events["requests"]) == 1 and len(events["deletes"]) == 1
    assert not capsys.readouterr().out


def test_cleanup_failure_recorded(settings, selection_fixture, capsys):
    _, _, control, _ = selection_fixture
    control["fail_delete"] = True
    result = call(settings, selection_fixture)
    assert result["receipt"]["remote_file_cleanup"] == "incomplete"
    assert "files/synthetic" not in capsys.readouterr().out


def test_unprocessed_video_never_generates(settings, selection_fixture):
    _, _, control, events = selection_fixture
    control["file_state"] = types.FileState.PROCESSING
    with pytest.raises(PipelineError):
        call(settings, selection_fixture)
    assert not events["requests"]
    assert len(events["deletes"]) == 1


def test_missing_key_stops_before_upload(settings, selection_fixture, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(PipelineError):
        call(settings, selection_fixture)
    assert not selection_fixture[3]["uploads"]
