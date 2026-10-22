import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import audio_first, pipeline, signals, worker_audio as worker
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json

URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
IDENTITY = "b" * 32


@pytest.fixture
def previous_analysis(tmp_path, monkeypatch):
    previous = tmp_path / "previous"
    previous.mkdir()
    audio = previous / "audio.m4a"
    audio.write_bytes(b"synthetic acquired audio")
    info = {"audio": True, "duration": 300, "audio_start": 0}
    settings = Settings.model_validate_json(
        Path("config.landscape.example.json").read_text()
    )
    key = pipeline.transcript_identity(audio, info, settings)
    write_json(
        previous / "transcripts" / (key + ".json"),
        {
            "language": "ja",
            "segments": [
                {
                    "start": n,
                    "end": n + 20,
                    "text": "合成の話題",
                    "words": [{"start": n, "end": n + 20, "text": "合成の話題"}],
                }
                for n in (0, 100, 200)
            ],
        },
    )
    write_json(previous / "asr-receipt.json", {"key": key, "reused": False})
    write_json(previous / "topics.json", {"topics": []})
    manifest = {
        "status": "topics_allocated",
        "model_generation_requests": 0,
        "source_url": URL,
        "source_sha256": digest(audio),
    }
    write_json(previous / "manifest.json", manifest)
    files = [
        "audio.m4a",
        "asr-receipt.json",
        "topics.json",
        "manifest.json",
        "transcripts/" + key + ".json",
    ]
    receipt = {
        "request_id": IDENTITY,
        "status": "topics_allocated",
        "audio_source_url": URL,
        "sha256": {name: digest(previous / name) for name in files},
    }
    write_json(previous / "request-receipt.json", receipt)
    monkeypatch.setattr(audio_first, "audio_info", lambda path: info)
    monkeypatch.setattr(
        signals,
        "transcribe",
        lambda *a, **k: pytest.fail("a downstream stage started ASR"),
    )
    return previous, tmp_path / "output", settings


def test_restore_verifies_original_audio_and_private_analysis(previous_analysis):
    previous, output, _ = previous_analysis
    output.mkdir()
    assert worker.restore_analysis(previous, output, IDENTITY) == URL
    assert (output / "audio.m4a").read_bytes() == (previous / "audio.m4a").read_bytes()
    assert not (output / "manifest.json").exists()
    assert not (output / "request-receipt.json").exists()


@pytest.mark.parametrize(
    "fault", ["wrong_id", "file", "source", "missing_hash", "traversal"]
)
def test_restore_refuses_mismatched_analysis(previous_analysis, fault):
    previous, output, _ = previous_analysis
    output.mkdir()
    receipt_path = previous / "request-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    if fault == "wrong_id":
        receipt["request_id"] = "c" * 32
    elif fault == "file":
        (previous / "audio.m4a").write_bytes(b"different")
    elif fault == "source":
        receipt["audio_source_url"] = URL.replace("example", "other")
    elif fault == "missing_hash":
        receipt["sha256"].pop("audio.m4a")
    else:
        receipt["sha256"]["../escape"] = "a" * 64
    write_json(receipt_path, receipt)
    with pytest.raises(PipelineError):
        worker.restore_analysis(previous, output, IDENTITY)


def test_preview_stage_cannot_retranscribe_or_fetch_full_audio(
    previous_analysis, monkeypatch
):
    previous, output, settings = previous_analysis
    calls = []
    monkeypatch.setattr(
        worker, "KickVOD", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )

    def previews(vod, root, settings, analysis_result=None):
        calls.append(analysis_result)
        return {
            "status": "awaiting_video_review",
            "previews": [],
            "analysis_key": "key",
        }

    monkeypatch.setattr(audio_first, "previews", previews)
    monkeypatch.setattr(worker, "preview_contact_sheet", lambda *a: None)
    result = worker.execute(previous, output, IDENTITY, settings, "previews")
    assert result["asr_reused"] is True and result["model_generation_requests"] == 0
    assert calls[0][1].segments
    assert json.loads((output / "asr-receipt.json").read_text())["reused"] is True


def test_changed_asr_settings_stop_before_network_or_recognition(
    previous_analysis, monkeypatch
):
    previous, output, settings = previous_analysis
    settings.threads = 4
    monkeypatch.setattr(
        worker,
        "KickVOD",
        lambda *a, **k: pytest.fail("network started before cache validation"),
    )
    with pytest.raises(PipelineError, match="no new recognition"):
        worker.execute(previous, output, IDENTITY, settings, "previews")


def test_worker_main_downloads_only_a_completed_private_analysis(
    previous_analysis, monkeypatch, capsys
):
    import shutil

    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)

    def gh(*args):
        if args[:2] == ("run", "view"):
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + IDENTITY,
                }
            )
        destination = Path(args[args.index("--dir") + 1])
        shutil.copytree(previous, destination)
        return ""

    monkeypatch.setattr(worker, "gh", gh)

    def execute(previous, output, *args):
        output.mkdir()
        (output / "audio.m4a").write_bytes(b"synthetic")
        return {"status": "awaiting_video_review", "source_url": URL}

    monkeypatch.setattr(worker, "execute", execute)
    args = [
        "--stage",
        "previews",
        "--previous-run",
        "123",
        "--previous-request",
        IDENTITY,
        "--request-id",
        "c" * 32,
        "--source-ref",
        "a" * 40,
        "--output",
        str(output),
    ]
    assert worker.main(args) == 0
    receipt = json.loads((output / "request-receipt.json").read_text())
    assert receipt["asr_reused"] is True and receipt["audio_stage"] == "previews"
    assert receipt["previous_run_id"] == "123"
    assert receipt["previous_request_id"] == IDENTITY
    assert "request-receipt.json" not in receipt["sha256"]
    assert json.loads(capsys.readouterr().out)["model_generation_requests"] == 0
    monkeypatch.setenv("GITHUB_REPOSITORY", "public/wrong")
    assert worker.main(args) == 1


def test_contact_sheet_is_labelled_samples_not_full_video_attestation(
    tmp_path, monkeypatch
):
    from PIL import Image

    root = tmp_path
    (root / "previews").mkdir()
    (root / "previews/topic-001.mp4").write_bytes(b"synthetic preview")
    monkeypatch.setattr(worker.media, "probe", lambda *a: {"duration": 30})
    calls = []

    def process(command):
        calls.append(command)
        Image.new("RGB", (320, 180), "blue").save(command[-1])

    monkeypatch.setattr(worker, "process", process)
    worker.preview_contact_sheet(
        root,
        {"previews": [{"topic_id": "topic-001", "file": "previews/topic-001.mp4"}]},
    )
    assert len(calls) == 3
    assert Image.open(root / "preview-contact-sheet.jpg").size == (960, 208)
