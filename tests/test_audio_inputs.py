import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from clipforge.audio_inputs import verify_audio_artifact
from clipforge.storage import PipelineError

URL = "https://youtu.be/6ras3m-gFuc"


@pytest.fixture
def artifact(tmp_path):
    root = tmp_path / "verified"
    root.mkdir()
    audio = b"verified-private-test-audio"
    (root / "audio.m4a").write_bytes(audio)
    manifest = {
        "schema": 1,
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "workflow_run": 123,
        "source_url": URL,
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "audio_bytes": len(audio),
        "audio_info": {"audio": True, "duration": 10.0, "audio_start": 0},
        "source_has_video": True,
        "asr_started": False,
    }
    (root / "audio-source.json").write_text(json.dumps(manifest))
    expected = {
        "source_url": URL,
        "source_ref": "b" * 40,
        "request_id": "a" * 32,
        "workflow_run": 123,
        "max_bytes": 4096,
        "max_seconds": 20,
    }
    return root, manifest, expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "c" * 32),
        ("source_ref", "c" * 40),
        ("workflow_run", 456),
        ("source_url", "https://youtu.be/abcdefghijk"),
        ("audio_sha256", "0" * 64),
        ("audio_bytes", 0),
        ("schema", True),
        ("asr_started", True),
    ],
)
def test_artifact_tamper_and_identity_mismatch_are_rejected(artifact, field, value):
    root, manifest, expected = artifact
    manifest[field] = value
    (root / "audio-source.json").write_text(json.dumps(manifest))
    with pytest.raises(PipelineError, match="mismatch"):
        verify_audio_artifact(root, **expected)


def test_artifact_requires_integrity_duration_budget_and_regular_files(artifact):
    root, manifest, expected = artifact
    assert verify_audio_artifact(root, **expected) == manifest
    with pytest.raises(PipelineError):
        verify_audio_artifact(root, **{**expected, "max_seconds": 5})
    (root / "audio.m4a").write_bytes(b"tampered-but-valid-sizedxxx")
    with pytest.raises(PipelineError):
        verify_audio_artifact(root, **expected)
    (root / "audio.m4a").unlink()
    (root / "audio.m4a").symlink_to(root / "audio-source.json")
    with pytest.raises(PipelineError, match="regular files"):
        verify_audio_artifact(root, **expected)


def test_reuse_stages_new_identity_without_network_or_transport(
    artifact, tmp_path, monkeypatch
):
    root, manifest, _ = artifact
    repository = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(repository / "tools"))
    spec = importlib.util.spec_from_file_location(
        "acquire_audio_for_reuse", repository / "tools/acquire_audio_url.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        raise AssertionError("reuse must never acquire a URL or initialize WARP")

    monkeypatch.setattr(module, "open_source", forbidden)
    from clipforge import youtube_transport

    monkeypatch.setattr(youtube_transport, "verify_ready", forbidden)
    output = tmp_path / "staged"
    module.main(
        [
            "--url",
            URL,
            "--request-id",
            "c" * 32,
            "--source-ref",
            "d" * 40,
            "--settings-json",
            json.dumps(
                {
                    "asr_parts": 2,
                    "youtube_audio_transport": "warp",
                }
            ),
            "--directory",
            str(output),
            "--verified-audio-dir",
            str(root),
            "--verified-audio-run",
            "123",
            "--verified-audio-request",
            "a" * 32,
            "--verified-audio-source-ref",
            "b" * 40,
        ]
    )
    request = json.loads((output / "request.json").read_text())
    receipt = json.loads((output / "acquisition-report.json").read_text())
    assert request["request_id"] == "c" * 32
    assert request["source_ref"] == "d" * 40
    assert request["video_sha256"] == manifest["audio_sha256"]
    assert (output / "input-video.bin").read_bytes() == (
        root / "audio.m4a"
    ).read_bytes()
    assert (
        receipt["transport"] == "verified_artifact" and receipt["producer_run"] == 123
    )
    assert receipt["asr_started"] is False
