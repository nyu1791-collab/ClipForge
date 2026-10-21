import json
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import types

from clipforge.models import Candidate, Transcript
from clipforge.selection import choose
from clipforge.storage import PipelineError


@pytest.mark.parametrize("http_status", [200, 429])
def test_real_sdk_serializes_video_schema_and_disables_retries(
    tmp_path, monkeypatch, settings, http_status
):
    calls = []
    payload = {
        "clips": [
            {
                "candidate_id": "candidate-001",
                "start": 1,
                "end": 7,
                "title": "Synthetic highlight",
                "reason": "Visible scene change",
                "confidence": 0.9,
            }
        ]
    }

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url.path.endswith("/test-video-model:generateContent")
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert body["generationConfig"]["responseSchema"]["type"] == "OBJECT"
        parts = body["contents"][0]["parts"]
        video = next(p for p in parts if "fileData" in p)
        assert (
            video["fileData"].get("mimeType", video["fileData"].get("mime_type"))
            == "video/mp4"
        )
        assert video["videoMetadata"]["fps"] == 2
        if http_status != 200:
            return httpx.Response(
                http_status,
                json={
                    "error": {
                        "code": 429,
                        "message": "Synthetic quota refusal",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": json.dumps(payload)}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "modelVersion": "test-video-model",
            },
        )

    real_client = genai.Client(
        api_key="dummy-for-mocked-transport",
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    deleted = []
    files = SimpleNamespace(
        upload=lambda **kw: types.File(
            name="files/test",
            uri="https://example.invalid/test",
            state=types.FileState.ACTIVE,
        ),
        delete=lambda **kw: deleted.append(kw),
    )
    monkeypatch.setattr(
        genai,
        "Client",
        lambda **kw: SimpleNamespace(
            files=files, models=real_client.models, close=real_client.close
        ),
    )
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-for-mocked-transport")
    path = tmp_path / "preview.mp4"
    path.write_bytes(b"synthetic")
    candidate = Candidate(
        id="candidate-001",
        start=20,
        end=30,
        heuristic_score=1,
        speech_seconds=10,
        chat_reactions=0,
        scene_boundaries=1,
    )
    if http_status == 200:
        result = choose(
            None,
            [path],
            [candidate],
            Transcript(language="en", segments=[]),
            {"duration": 40},
            settings,
        )
        assert result["clips"][0]["start"] == 21
    else:
        with pytest.raises(PipelineError, match="HTTP 429"):
            choose(
                None,
                [path],
                [candidate],
                Transcript(language="en", segments=[]),
                {"duration": 40},
                settings,
            )
    assert len(calls) == 1
    assert len(deleted) == 1
