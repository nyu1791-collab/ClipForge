"""Offline contract checks for selected GPU audio and Gemini subtitle requests."""

import base64
import importlib
import json
import math
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from clipforge import asr, audio_understanding as route
from clipforge.models import SelectedClip, Settings
from clipforge.speaker_captions import speaker_for_token
from clipforge.storage import PipelineError


@pytest.fixture
def clip():
    return SelectedClip(start=10, end=20, candidate_id="a", title="話題", reason="会話", confidence=0.9)


@pytest.fixture
def raw():
    return {"segments": [
        {"start": 1, "end": 2, "text": "うん"},
        {"start": 3, "end": 4, "text": "誤認"},
        {"start": 5, "end": 6, "text": "短い声"},
    ]}


def test_cleanup_keeps_gpu_times_and_safe_speaker_assignments(tmp_path, clip, raw):
    audio = tmp_path / "selected.ogg"
    audio.write_bytes(b"audio")

    class Client:
        def generate(self, phase, prompt, audio, audio_source_key):
            assert phase == "transcript-1"
            assert audio.read_bytes() == b"audio"
            assert audio_source_key == "source-key"
            request = json.loads(prompt)
            assert request["source_segments"] == [dict(id=i, **row) for i, row in enumerate(raw["segments"], 1)]
            return {"audio_access": "heard", "segments": [
                {"source_ids": [1], "text": "うん", "speaker_id": "speaker_2", "confidence": 0.95},
                {"source_ids": [2], "text": "", "omit_non_speech": True},
                {"source_ids": [3], "text": "短い声", "speaker_id": "speaker_1", "confidence": 0.6},
            ]}, {}

    result = route.clean_selected_clip(Client(), raw, 1, clip, [], audio, "source-key")
    assert [(s["start"], s["end"], s["text"]) for s in result["segments"]] == [(1, 2, "うん"), (5, 6, "短い声")]
    transcript, turns = route.selected_transcript(result, 10)
    assert [(s.start, s.end) for s in transcript.segments] == [(1, 2), (5, 6)]
    assert speaker_for_token(1, 2, turns) == "speaker_2"
    assert speaker_for_token(5, 6, turns) is None


@pytest.mark.parametrize("audio_access", ["unavailable", None])
def test_cleanup_fails_if_model_cannot_hear_audio(tmp_path, clip, raw, audio_access):
    class Client:
        def generate(self, *args, **kwargs):
            return {"audio_access": audio_access, "segments": []}, {}

    with pytest.raises(PipelineError, match="audio was not available"):
        route.clean_selected_clip(Client(), raw, 1, clip, [], tmp_path / "selected.ogg")


def test_vertex_request_contains_audio_json_rules_and_reuses_the_same_source(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "fake")
    calls = []
    result = {"audio_access": "heard", "segments": []}

    def fake_generate(model, thinking, system_text, parts, research=False, max_tokens=65536):
        calls.append((model, thinking, system_text, parts))
        return json.dumps(result)

    monkeypatch.setattr(route, "generate_text", fake_generate)
    audio = tmp_path / "selected.ogg"
    audio.write_bytes(b"fake ogg payload")
    first = tmp_path / "first"
    first.mkdir()
    client = route.Client(first)
    value, _ = client.generate("transcript-1", '{"source_segments":[]}', audio=audio, audio_source_key="same-source")
    assert value == result
    model, thinking, system_text, parts = calls[0]
    assert model == "gemini-3.5-flash"
    assert thinking == "MEDIUM"
    assert "unknown" in system_text
    assert json.loads(parts[0]["text"].split("\n\n")[0])["source_segments"] == []
    attached = next(p["inlineData"] for p in parts if "inlineData" in p)
    assert attached["mimeType"] == "audio/ogg"
    assert base64.b64decode(attached["data"]) == b"fake ogg payload"
    assert client.record["requests"][0]["audio_source_key"] == "same-source"
    # A fresh Ogg encoding may have different container bytes but the verified
    # source digest, clip bounds and encoding configuration still identify it.
    audio.write_bytes(b"fresh encoding of same source")
    replay = tmp_path / "replay"
    replay.mkdir()
    resumed = route.Client(replay, resume=first)
    assert resumed.generate("transcript-1", '{"source_segments":[]}', audio=audio, audio_source_key="same-source")[0] == result
    assert len(calls) == 1
    with pytest.raises(PipelineError, match="No complete saved response"):
        resumed.generate("transcript-1", '{"source_segments":[]}', audio=audio, audio_source_key="different-source")
    assert len(calls) == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_asr_copy_amplifies_quiet_audio_without_changing_duration(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    selected = importlib.import_module("selected_gpu_asr")
    rate = asr.SAMPLE_RATE
    t = np.arange(rate * 4) / rate
    original = (0.01 * np.sin(2 * math.pi * 440 * t) * 32767).astype("<i2")
    original[:rate] = 0
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(original.tobytes())
    work = tmp_path / "work"
    work.mkdir()
    prepared, mappings = selected.prepare_audio(source, [{"start": 0, "end": 4}], work)
    with wave.open(str(prepared), "rb") as wav:
        assert wav.getnframes() == rate * 4
        changed = np.frombuffer(wav.readframes(rate * 4), dtype="<i2")
    assert mappings == [{"start": 0.0, "end": 4.0}]
    assert np.max(np.abs(changed[rate * 2:rate * 3])) > np.max(np.abs(original[rate * 2:rate * 3])) * 2
    assert not np.any(changed[:rate // 2])
    with wave.open(str(source), "rb") as wav:
        assert wav.readframes(rate * 4) == original.tobytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_gpu_and_gemini_audio_keep_the_same_clip_clock(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    selected = importlib.import_module("selected_gpu_asr")
    rate = asr.SAMPLE_RATE
    samples = np.zeros(rate * 6, dtype="<i2")
    pulse = np.arange(int(rate * 0.6)) / rate
    samples[int(rate * 2.5):int(rate * 3.1)] = (0.05 * np.sin(2 * math.pi * 440 * pulse) * 32767).astype("<i2")
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(samples.tobytes())
    work = tmp_path / "work"
    work.mkdir()
    gpu_audio, mappings = selected.prepare_audio(source, [{"start": 2, "end": 5}], work)
    gemini_audio = tmp_path / "selected.ogg"
    route.encode_audio(source, gemini_audio, 2, 3, "24k", speech_fast=True)
    with wave.open(str(gpu_audio), "rb") as wav:
        gpu = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(gemini_audio), "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True, check=True,
    ).stdout
    gemini = np.frombuffer(decoded, dtype="<i2")
    assert mappings == [{"start": 0.0, "end": 3.0}]
    assert len(gpu) == len(gemini) == 3 * rate
    for signal in (gpu, gemini):
        onset = np.flatnonzero(np.abs(signal.astype(np.int32)) > 500)[0] / rate
        assert onset == pytest.approx(0.5, abs=0.05)


def test_prior_asr_version_is_not_resubmitted(tmp_path):
    resume = tmp_path / "resume"
    resume.mkdir()
    (resume / "selected-gpu-asr.json").write_text(json.dumps({"input_key": "old", "status": "complete"}))
    output = tmp_path / "out"
    output.mkdir()
    clips = [SelectedClip(start=0, end=2, candidate_id="a", title="a", reason="b", confidence=1)]
    with pytest.raises(PipelineError, match="do not resubmit"):
        route.selected_gpu_transcripts(tmp_path / "input", clips, Settings(), output, resume, True, "abc")


def test_reprocess_uses_saved_intervals_and_starts_new_asr(tmp_path, monkeypatch):
    resume = tmp_path / "parent"
    resume.mkdir()
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"new source")
    settings = Settings()
    url = "https://kick.com/example/videos/01a171d4-2920-7e7b-a842-e2b580698688"
    rows = [{"start": 10, "end": 20, "candidate_id": "topic-0", "title": "話題", "reason": "会話", "confidence": 0.9}]
    (resume / "audio-editor-receipt.json").write_text(json.dumps({"input_identity": {"source_url": url, "audio_sha256": "old", "settings": settings.model_dump()}, "duration": 30}))
    (resume / "selected-gpu-asr-plan.json").write_text(json.dumps({"source_audio_sha256": "old", "clips": [{"start": 10, "end": 20}]}))
    (resume / "final-selection.json").write_text(json.dumps({"source_url": url, "clips": rows}))
    (resume / "terminology-glossary.json").write_text(json.dumps({"terms": []}))
    monkeypatch.setattr(route.audio_first, "audio_info", lambda _: {"duration": 30})
    monkeypatch.setattr(route, "validate_reacquired_source", lambda *args: {"method": "matched"})
    selected = []

    def process(audio, passed_url, output, passed_settings, prior, continue_missing, identity, client, clips, glossary):
        assert prior is None and continue_missing is False
        selected.extend(c.model_dump() for c in clips)

    monkeypatch.setattr(route, "process_selected", process)
    output = tmp_path / "new"
    route.reprocess_saved(source, url, output, settings, resume)
    assert selected == rows
    receipt = json.loads((output / "audio-editor-receipt.json").read_text())
    assert receipt["status"] == "complete"
    assert receipt["reprocessed_selection"]["parent_audio_sha256"] == "old"


def test_reprocess_rejects_mismatched_saved_intervals(tmp_path, monkeypatch):
    resume = tmp_path / "parent"
    resume.mkdir()
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"source")
    settings = Settings()
    url = "https://kick.com/example/videos/01a171d4-2920-7e7b-a842-e2b580698688"
    rows = [{"start": 10, "end": 20, "candidate_id": "topic-0", "title": "話題", "reason": "会話", "confidence": 0.9}]
    (resume / "audio-editor-receipt.json").write_text(json.dumps({"input_identity": {"source_url": url, "audio_sha256": "old", "settings": settings.model_dump()}, "duration": 30}))
    (resume / "selected-gpu-asr-plan.json").write_text(json.dumps({"source_audio_sha256": "old", "clips": [{"start": 11, "end": 20}]}))
    (resume / "final-selection.json").write_text(json.dumps({"source_url": url, "clips": rows}))
    with pytest.raises(PipelineError, match="intervals differ"):
        route.reprocess_saved(source, url, tmp_path / "new", settings, resume)
