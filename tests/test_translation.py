import json
import subprocess
from pathlib import Path

import pytest
from clipforge.storage import PipelineError
from translation.backend import Vertex
from translation.captions import apply_translation, make_cues, write_subtitles
from translation.cli import DEFAULTS
from translation.pipeline import run, windows


def test_cues_keep_word_time_and_natural_english_spacing():
    rows = [
        {"start": 0.2, "end": 0.5, "text": "Hello", "source_speaker": "one"},
        {"start": 0.5, "end": 0.8, "text": "world", "source_speaker": "one"},
        {"start": 1.0, "end": 1.4, "text": "Hi", "source_speaker": "two"},
    ]
    cues = make_cues(rows, 0)
    assert [(c["text"], c["start"], c["end"]) for c in cues] == [
        ("Hello world", 0.2, 0.8),
        ("Hi", 1.0, 1.4),
    ]


def test_missing_and_duplicate_translation_ids_keep_the_correct_source():
    cues = [
        {"id": "0-1", "text": "one", "start": 0, "end": 1},
        {"id": "0-2", "text": "two", "start": 2, "end": 3},
        {"id": "0-3", "text": "three", "start": 4, "end": 5},
    ]
    response = {
        "segments": [
            {"id": "0-1", "text": "一"},
            {"id": "0-2", "text": "二"},
            {"id": "0-2", "text": "二つ"},
        ]
    }
    result, missing = apply_translation(cues, response)
    assert [r["translation"] for r in result] == ["一", "two", "three"]
    assert missing == ["0-2", "0-3"]
    assert [(r["start"], r["end"]) for r in result] == [(0, 1), (2, 3), (4, 5)]


def test_overlap_window_ownership():
    assert windows(900) == [0, 896]
    assert windows(898) == [0]


def test_subtitle_markup_is_escaped_and_bilingual(tmp_path):
    cues = [
        {
            "start": 0.2,
            "end": 1.0,
            "text": "Hi",
            "translation": "こんにちは {x}\\Nno",
            "translated": True,
        }
    ]
    write_subtitles(cues, tmp_path, bilingual=True)
    text = (tmp_path / "subtitles.ass").read_text()
    assert "（x）＼Nno\\NHi" in text
    assert (tmp_path / "subtitles.srt").exists()


def test_vertex_payload_uses_detected_language_and_source_specific_cache(
    tmp_path, monkeypatch
):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"audio")
    client = Vertex("sample", tmp_path / "cache")
    captured = []

    def fake(model, body, cache):
        captured.append((model, body, cache))
        return {}

    monkeypatch.setattr(client, "generate", fake)
    client.transcribe(audio, "", {"source_sha256": "a"})
    client.transcribe(audio, "en-US", {"source_sha256": "b"})
    assert (
        captured[0][1]["generationConfig"]["audioTranscriptionConfig"]["languageCodes"]
        == []
    )
    assert captured[1][1]["generationConfig"]["audioTranscriptionConfig"][
        "languageCodes"
    ] == ["en-US"]
    assert captured[0][2]["source_sha256"] != captured[1][2]["source_sha256"]


def test_full_video_flow_with_synthetic_input_and_mock_models(tmp_path, monkeypatch):
    video = tmp_path / "input.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:r=12:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        check=True,
    )

    class FakeVertex:
        def __init__(self, project, cache):
            pass

        def transcribe(self, audio, language, cache_identity):
            return {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "audioTranscription": {
                                        "speakerLabel": "a",
                                        "words": [
                                            {
                                                "word": "Hello",
                                                "startOffset": "0.2s",
                                                "endOffset": "0.5s",
                                            },
                                            {
                                                "word": "world",
                                                "startOffset": "0.5s",
                                                "endOffset": "0.9s",
                                            },
                                        ],
                                    }
                                }
                            ]
                        },
                    }
                ]
            }

        def translate(self, cues, target, model, thinking):
            return {"segments": [{"id": c["id"], "text": "こんにちは"} for c in cues]}

    monkeypatch.setattr("translation.pipeline.Vertex", FakeVertex)
    options = {
        **DEFAULTS,
        "project": "test",
        "start": 0.5,
        "end": 2.5,
        "height": 240,
        "gap_recovery": False,
    }
    out = tmp_path / "out"
    result = run(str(video), out, options)
    assert result["status"] == "complete" and result["untranslated_count"] == 0
    assert all(
        (out / f).is_file()
        for f in ["video.mp4", "subtitles.srt", "subtitles.ass", "translation.json"]
    )
    cue = json.loads((out / "translation.json").read_text())["cues"][0]
    assert cue["text"] == "Hello world" and cue["translation"] == "こんにちは"
    assert (cue["start"], cue["end"]) == (0.2, 0.9)
    assert run(str(video), out, options)["run_key"] == result["run_key"]
    with pytest.raises(PipelineError, match="different settings"):
        run(str(video), out, {**options, "target_language": "français"})


def test_active_gap_recovery_uses_original_timing(tmp_path, monkeypatch):
    from translation.pipeline import process_window

    calls = []

    class Fake:
        def transcribe(self, audio, language, cache_identity):
            calls.append(cache_identity)
            seconds = "1s" if len(calls) == 1 else "3s"
            return {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "audioTranscription": {
                                        "speakerLabel": "spk_1",
                                        "words": [
                                            {
                                                "word": "hello",
                                                "startOffset": seconds,
                                                "endOffset": "4s",
                                            }
                                        ],
                                    }
                                }
                            ]
                        },
                    }
                ]
            }

        def translate(self, cues, target, model, thinking):
            return {"segments": [{"id": c["id"], "text": "こんにちは"} for c in cues]}

    monkeypatch.setattr(
        "translation.pipeline.encode_audio",
        lambda video, path, start, duration: path.write_bytes(b"fake"),
    )
    monkeypatch.setattr(
        "translation.pipeline.describe_gaps",
        lambda *a: {
            "gaps": [{"start": 10, "end": 30, "peak_one_second_rms_dbfs": -20}]
        },
    )
    options = {**DEFAULTS, "project": "test", "gap_recovery": True}
    result = process_window(
        Fake(), Path("unused"), "sha", 100, 40, 200, 50, 0, 1, options
    )
    assert len(calls) == 2
    assert calls[1]["offset"] == 108
    assert result["gap_recovery"] == [{"start": 110, "end": 130, "status": "received"}]
    assert [c["start"] for c in result["cues"]] == [101, 111]


def test_vertex_caches_same_request_and_rejects_invalid_translation_json(
    tmp_path, monkeypatch
):
    import io
    import urllib.request

    monkeypatch.setenv("TRANSLATION_VERTEX_ACCESS_TOKEN", "temporary-test-token")
    calls = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def send(request, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return Response(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "content": {"parts": [{"text": "not-json"}]},
                            }
                        ]
                    }
                ).encode()
            )
        return Response(
            json.dumps(
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {"text": '{"segments":[{"id":"0-1","text":"猫"}]}'}
                                ]
                            },
                        }
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", send)
    client = Vertex("sample", tmp_path / "cache")
    rows = [{"id": "0-1", "text": "cat"}]
    with pytest.raises(PipelineError, match="not valid JSON"):
        client.translate(rows, "日本語", "gemini-3.5-flash", "MEDIUM")
    result = client.translate(rows, "日本語", "gemini-3.5-flash", "MEDIUM")
    assert result["segments"][0]["text"] == "猫"
    assert client.translate(rows, "日本語", "gemini-3.5-flash", "MEDIUM") == result
    assert len(calls) == 2
    assert calls[1]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "MEDIUM"
