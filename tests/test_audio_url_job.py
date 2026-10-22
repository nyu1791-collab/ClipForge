import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import audio_url_job as job

URL = "https://kick.com/test-channel/videos/01234567-89ab-cdef-0123-456789abcdef"
KEY = "c" * 64


@pytest.fixture
def audio_job(tmp_path, monkeypatch):
    output = tmp_path / "out"
    analysis = {
        "status": "topics_allocated",
        "source_url": URL,
        "duration": 60,
        "topics": [],
        "model_generation_requests": 0,
    }
    vod = Mock()

    def kick_vod(url, segments, **kwargs):
        segments.mkdir()
        (segments / "temporary.ts").touch()
        return vod

    def acquire(vod, root):
        # An empty placeholder exercises file hashing without generating any audio.
        (root / "audio.m4a").touch()

    def allocate(audio, root, settings, url):
        (root / "topics.json").write_text(json.dumps(analysis))
        (root / "asr-receipt.json").write_text(json.dumps({"key": KEY}))
        (root / "transcripts").mkdir()
        (root / "transcripts" / (KEY + ".json")).write_text('{"segments": []}')
        return analysis, None

    constructor = Mock(side_effect=kick_vod)
    acquisition = Mock(side_effect=acquire)
    allocation = Mock(side_effect=allocate)
    monkeypatch.setattr(job, "KickVOD", constructor)
    monkeypatch.setattr(job.audio_first, "acquire", acquisition)
    monkeypatch.setattr(job.audio_first, "allocate_audio", allocation)
    monkeypatch.setattr(job.os, "umask", Mock())
    arguments = [
        "--url",
        URL,
        "--request-id",
        "a" * 32,
        "--source-ref",
        "b" * 40,
        "--output",
        str(output),
        "--workflow-run",
        "123",
    ]
    return output, analysis, vod, constructor, acquisition, allocation, arguments


def test_audio_url_job_matches_audio_analysis_receipts(audio_job, capsys):
    output, analysis, vod, constructor, acquisition, allocation, arguments = audio_job
    job.main(arguments)
    settings = allocation.call_args.args[2]
    configured = job.Settings.model_validate_json(
        (
            Path(job.__file__).resolve().parents[1] / "config.landscape.example.json"
        ).read_text()
    ).model_copy(update={"transcript_cache": str(output / "transcripts")})
    assert settings == configured
    constructor.assert_called_once_with(
        URL,
        output / "segments",
        max_bytes=settings.max_input_bytes,
        max_seconds=settings.max_source_seconds,
    )
    acquisition.assert_called_once_with(vod, output)
    allocation.assert_called_once_with(output / "audio.m4a", output, settings, URL)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest == {
        **analysis,
        "rendered": [],
        "source_sha256": hashlib.sha256(b"").hexdigest(),
    }
    names = [
        "audio.m4a",
        "manifest.json",
        "topics.json",
        "asr-receipt.json",
        "transcripts/" + KEY + ".json",
    ]
    receipt_path = output / "request-receipt.json"
    assert json.loads(receipt_path.read_text()) == {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "workflow_run": "123",
        "status": "topics_allocated",
        "rendered_clips": 0,
        "audio_source_url": URL,
        "sha256": {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in names
        },
    }
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert not (output / "segments").exists()
    vod.close.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) == {
        "status": "topics_allocated",
        "clips": 0,
    }


@pytest.mark.parametrize("stage", ["constructor", "acquire", "allocate"])
def test_audio_url_job_removes_segments_even_on_failure(audio_job, stage):
    output, _, vod, constructor, acquisition, allocation, arguments = audio_job
    if stage == "constructor":

        def fail_constructor(url, segments, **kwargs):
            segments.mkdir()
            (segments / "partial.ts").touch()
            raise RuntimeError("test failure")

        constructor.side_effect = fail_constructor
    else:
        target = acquisition if stage == "acquire" else allocation
        target.side_effect = RuntimeError("test failure")
    with pytest.raises(RuntimeError, match="test failure"):
        job.main(arguments)
    assert not (output / "segments").exists()
    assert not (output / "request-receipt.json").exists()
    if stage != "constructor":
        vod.close.assert_called_once_with()
    if stage == "allocate":
        assert (output / "audio.m4a").is_file()


def test_audio_url_job_rejects_invalid_url_before_acquisition(audio_job):
    output, _, _, constructor, acquisition, allocation, arguments = audio_job
    arguments[1] = URL + "?query=1"
    with pytest.raises(job.audio_first.PipelineError, match="canonical Kick VOD"):
        job.main(arguments)
    constructor.assert_not_called()
    acquisition.assert_not_called()
    allocation.assert_not_called()
    assert not output.exists()
