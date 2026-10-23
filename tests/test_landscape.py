import copy
import json
from pathlib import Path

import pytest

from clipforge import landscape, pipeline
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json


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


def test_landscape_defaults_and_invalid_reading_settings(landscape_settings):
    assert landscape_settings.aspect == "16:9" and landscape_settings.height == 1080
    assert (
        landscape_settings.horizontal_target_cps
        == landscape_settings.horizontal_warn_cps
        == 7
    )
    assert landscape_settings.horizontal_line_chars == 13
    assert landscape_settings.gemini_model == "manual-no-api"
    with pytest.raises(ValueError):
        Settings.model_validate(
            {**landscape_settings.model_dump(), "horizontal_target_cps": 8}
        )
    with pytest.raises(ValueError):
        Settings(gemini_model="manual-no-api", require_transcript_cache=True)


@pytest.mark.parametrize(
    "fault", ["too_few", "too_many", "overlap", "outside", "empty_title"]
)
def test_selection_rejects_incomplete_or_ambiguous_delivery(
    manual_plan, landscape_settings, fault
):
    if fault == "too_few":
        del manual_plan["clips"][1:]
    elif fault == "too_many":
        manual_plan["clips"] *= 2
    elif fault == "overlap":
        manual_plan["clips"][1].update(start=50, end=110)
    elif fault == "outside":
        manual_plan["clips"][2]["end"] = 1000
    else:
        manual_plan["clips"][0]["title"] = " "
    with pytest.raises((PipelineError, ValueError)):
        landscape.validate_selection(manual_plan, landscape_settings, 300)


def test_two_clips_are_a_valid_landscape_delivery(manual_plan, landscape_settings):
    manual_plan["clips"].pop()
    landscape_settings = landscape_settings.model_copy(update={"clips": 2})
    landscape.validate_selection(manual_plan, landscape_settings, 300)


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


@pytest.mark.parametrize(
    "fault",
    ["aspect", "height", "quality", "network", "audio", "duration", "empty", "cache"],
)
def test_preflight_refuses_before_processing(
    local_input, landscape_settings, monkeypatch, fault
):
    source, selection, _ = local_input
    if fault == "aspect":
        landscape_settings.aspect = "9:16"
    elif fault == "height":
        landscape_settings.height = 720
    elif fault == "quality":
        landscape_settings.quality_gate = "warn"
    elif fault == "network":
        landscape_settings.image_search = True
    elif fault in {"audio", "duration"}:
        monkeypatch.setattr(
            landscape.media,
            "probe",
            lambda p: {"audio": fault != "audio", "duration": 100000},
        )
    elif fault == "empty":
        source.write_bytes(b"")
    else:
        landscape_settings.transcript_cache = "unused"
        landscape_settings.require_transcript_cache = True
        monkeypatch.setattr(pipeline, "saved_transcript", lambda *a: None)
    with pytest.raises(PipelineError):
        landscape.preflight(source, selection, landscape_settings)


def test_default_runner_only_prepares_and_preserves_input(
    local_input, monkeypatch, capsys
):
    source, selection, output = local_input
    original = digest(source)
    monkeypatch.setattr(
        pipeline, "run", lambda *a, **kw: pytest.fail("prepare rendered a clip")
    )
    assert (
        landscape.main(
            [
                "--input",
                str(source),
                "--selection-file",
                str(selection),
                "--output",
                str(output),
                "--transcript-cache",
                "unused",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "prepared" and result["planned_clips"] == 3
    assert result["model_generation_requests"] == 0
    assert result["transcript_cache_available"] is True
    assert digest(source) == original and not output.exists()


def test_runner_redacts_private_error(local_input, monkeypatch, capsys):
    source, selection, output = local_input

    def fail(*args):
        raise PipelineError("PRIVATE_SPEECH")

    monkeypatch.setattr(landscape, "preflight", fail)
    assert (
        landscape.main(
            [
                "--input",
                str(source),
                "--selection-file",
                str(selection),
                "--output",
                str(output),
                "--allow-asr",
            ]
        )
        == 1
    )
    assert "PRIVATE_SPEECH" not in capsys.readouterr().err


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


def test_delivery_verifies_files_but_does_not_certify_topic_quality(delivery):
    manifest, output = delivery
    summary = landscape.summarize_delivery(manifest, output, 3)
    assert summary["rendered_clips"] == 3
    assert summary["topic_content_review"] == "required"
    assert summary["human_review_performed"] is False
    assert "PRIVATE_SPEECH" not in json.dumps(summary)


@pytest.mark.parametrize(
    "fault",
    [
        "count",
        "changed_file",
        "width",
        "audio_rate",
        "quality",
        "stored_report",
        "provider",
        "generation",
        "traversal",
    ],
)
def test_delivery_rejects_wrong_or_tampered_results(delivery, fault):
    manifest, output = delivery
    manifest = copy.deepcopy(manifest)
    row = manifest["rendered"][0]
    if fault == "count":
        manifest["rendered"].pop()
    elif fault == "changed_file":
        (output / row["directory"] / "video.mp4").write_bytes(b"changed")
    elif fault == "width":
        row["media"]["width"] = 404
    elif fault == "audio_rate":
        row["media"]["audio_sample_rate"] = 96000
    elif fault == "quality":
        row["quality"]["passed"] = False
    elif fault == "stored_report":
        row["quality"]["warnings"] = []
    elif fault == "provider":
        manifest["selection_receipt"]["provider"] = "gemini"
    elif fault == "generation":
        manifest["selection_receipt"]["generation_requests"] = 1
    else:
        row["directory"] = "../outside"
    with pytest.raises(PipelineError):
        landscape.summarize_delivery(manifest, output, 3)


def test_execute_records_verified_numeric_summary(
    local_input, delivery, monkeypatch, capsys
):
    source, selection, _ = local_input
    manifest, output = delivery
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return manifest

    monkeypatch.setattr(pipeline, "run", run)
    assert (
        landscape.main(
            [
                "--input",
                str(source),
                "--selection-file",
                str(selection),
                "--output",
                str(output),
                "--transcript-cache",
                "unused",
                "--execute",
            ]
        )
        == 0
    )
    assert calls[0][1]["selection_file"] == selection
    assert calls[0][0][2].require_transcript_cache is True
    summary = json.loads((output / "e2e-summary.json").read_text())
    assert summary["selection_sha256"] == digest(selection)
    assert summary["reproducibility"]["implementation_sha256"]
    assert "PRIVATE_SPEECH" not in json.dumps(summary)
    assert json.loads(capsys.readouterr().out)["rendered_clips"] == 3
