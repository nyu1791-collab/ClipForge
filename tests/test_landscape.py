import json
from pathlib import Path

import pytest

from clipforge import landscape, pipeline
from clipforge.models import Settings
from clipforge.storage import digest, write_json


@pytest.fixture
def landscape_settings():
    return Settings.model_validate_json(
        Path("config.landscape.example.json").read_text()
    )


@pytest.fixture
def manual_plan():
    return {
        "clips": [
            {
                "start": n * 80,
                "end": n * 80 + 60,
                "title": f"topic {n}",
                "reason": "synthetic interval",
                "candidate_id": f"manual-{n}",
                "confidence": 1,
            }
            for n in range(3)
        ]
    }


def test_safe_summary_never_copies_speech_paths_or_unrecognised_metrics():
    report = {
        "passed": True,
        "failures": [],
        "warnings": ["PRIVATE_SPEECH"],
        "metrics": {
            "caption_count": 3,
            "caption_max_cps": float("nan"),
            "private_path": "/private/input.mp4",
            "examples": ["PRIVATE_SPEECH"],
            "captions_over_cps": {"4": 3, "7": 1, "10": 0, "private": "SECRET"},
        },
    }
    summary = landscape.safe_quality_summary(report)
    assert summary["warning_count"] == 1
    assert summary["metrics"]["captions_over_cps"] == {"4": 3, "7": 1, "10": 0}
    assert "caption_max_cps" not in summary["metrics"]
    assert not any(
        word in json.dumps(summary)
        for word in ("PRIVATE", "/private", "SECRET", "examples")
    )


@pytest.fixture
def local_input(tmp_path, monkeypatch, manual_plan):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic source")
    selection = tmp_path / "selection.json"
    write_json(selection, manual_plan)
    monkeypatch.setattr(
        landscape.media, "probe", lambda p: {"audio": True, "duration": 300}
    )
    monkeypatch.setattr(
        pipeline, "saved_transcript", lambda *a: {"language": "ja", "segments": []}
    )
    return source, selection, tmp_path / "output"


@pytest.fixture
def delivery(tmp_path):
    output = tmp_path / "out"
    rows = []
    for n in range(3):
        folder = output / "clips" / f"clip-{n + 1:03}"
        folder.mkdir(parents=True)
        report = {
            "passed": True,
            "failures": [],
            "warnings": ["PRIVATE_SPEECH"],
            "metrics": {
                "caption_count": 1,
                "captions_over_cps": {"4": 1, "7": 0, "10": 0},
            },
        }
        for name in pipeline.REQUIRED_FILES:
            (folder / name).write_bytes(b"synthetic fixture")
        write_json(folder / "ledger.json", [])
        write_json(folder / "quality.json", report)
        rows.append(
            {
                "directory": f"clips/clip-{n + 1:03}",
                "key": "test-key",
                "ledger": [],
                "media": {"width": 1920, "height": 1080, "audio_sample_rate": 48000},
                "quality": report,
                "sha256": {
                    name: digest(folder / name) for name in pipeline.REQUIRED_FILES
                },
            }
        )
    manifest = {
        "status": "complete",
        "selection_receipt": {"provider": "manual", "generation_requests": 0},
        "rendered": rows,
        "transcript_cache": {
            "enabled": True,
            "key": "hash",
            "reused": True,
            "source": "transcript_cache",
        },
    }
    return manifest, output
