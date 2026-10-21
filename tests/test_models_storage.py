import json
import subprocess

import pytest
from pydantic import ValidationError

from clipforge.models import SelectedClip, Settings, Span, validate_picks
from clipforge.storage import (
    PipelineError,
    digest,
    fingerprint,
    process,
    stage,
    within,
    write_json,
)


@pytest.mark.parametrize(
    "start,end", [(0, 0), (3, 2), (-1, 2), (float("nan"), 3), (0, float("inf"))]
)
def test_invalid_spans(start, end):
    with pytest.raises(ValidationError):
        Span(start=start, end=end)


@pytest.mark.parametrize(
    "updates",
    [
        {"gemini_model": ""},
        {"gemini_model": "bad\nmodel"},
        {"gemini_model": "https://example.org/model"},
        {"clips": 0},
        {"clips": 11},
        {"clips": 4, "candidates": 3},
        {"threads": 0},
        {"threads": 9},
        {"aspect": "4:3"},
        {"height": 1000},
        {"framing": "face-track"},
        {"caption": "html"},
        {"analysis_fps": 0},
        {"analysis_fps": 6},
        {"max_input_bytes": 0},
        {"max_source_seconds": 7201},
        {"min_seconds": 10, "max_seconds": 10},
        {"font": "Noto\nInjected"},
        {"font": r"Noto\x"},
        {"font": "Noto,Arial"},
        {"max_seconds": 180, "analysis_fps": 5, "candidates": 10},
        {"unknown_option": True},
        {"chat_offset_seconds": 301},
    ],
)
def test_settings_reject_invalid_configuration(settings, updates):
    with pytest.raises(ValidationError):
        Settings.model_validate({**settings.model_dump(), **updates})


def pick(start=1, end=6, **overrides):
    return SelectedClip(
        start=start,
        end=end,
        candidate_id="manual",
        title="Title",
        reason="Evidence",
        confidence=0.8,
        **overrides,
    )


@pytest.mark.parametrize(
    "clips,duration",
    [
        ([], 30),
        ([pick(25, 31)], 30),
        ([pick(0, 2)], 30),
        ([pick(0, 12)], 30),
        ([pick(0, 6), pick(1, 7)], 30),
        ([pick(0, 4), pick(10, 14), pick(20, 24)], 30),
    ],
)
def test_pick_validation(settings, clips, duration):
    with pytest.raises(ValueError):
        validate_picks(clips, duration, settings)


def test_valid_touching_clips(settings):
    clips = [pick(0, 5), pick(5, 10)]
    assert validate_picks(clips, 30, settings) == clips


def test_private_atomic_json_and_hash(tmp_path):
    path = tmp_path / "private" / "record.json"
    write_json(path, {"日本語": [1, 2]})
    assert json.loads(path.read_text()) == {"日本語": [1, 2]}
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(digest(path)) == 64
    assert not list(path.parent.glob(".*"))
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_failed_json_does_not_overwrite(tmp_path):
    path = tmp_path / "record.json"
    write_json(path, {"valid": True})
    with pytest.raises(ValueError):
        write_json(path, {"invalid": float("nan")})
    assert json.loads(path.read_text()) == {"valid": True}
    assert not list(tmp_path.glob(".*"))


def test_stage_cache_and_corrupt_content(tmp_path):
    calls = []

    def produce():
        calls.append(1)
        return {"count": len(calls)}

    assert stage(tmp_path, "result", "a", produce) == {"count": 1}
    assert stage(tmp_path, "result", "a", produce) == {"count": 1}
    (tmp_path / "result.json").write_text("corrupt")
    assert stage(tmp_path, "result", "a", produce) == {"count": 2}
    assert stage(tmp_path, "result", "b", produce) == {"count": 3}


def test_corrupt_receipt_is_recoverable(tmp_path):
    stage(tmp_path, "result", "a", lambda: {"old": True})
    (tmp_path / ".state/result.json").write_text("{broken")
    assert stage(tmp_path, "result", "a", lambda: {"fresh": True}) == {"fresh": True}


def test_output_containment(tmp_path):
    assert within(tmp_path, tmp_path / "clips") == tmp_path / "clips"
    with pytest.raises(PipelineError):
        within(tmp_path, tmp_path / "../other")
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(PipelineError):
        within(tmp_path, tmp_path / "link" / "outside")


def test_process_never_echoes_child_errors(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 7, b"", b"private-content"),
    )
    with pytest.raises(PipelineError) as caught:
        process(["ffmpeg", "-i", "private-input"])
    assert str(caught.value) == "ffmpeg failed (exit 7)"
    assert "private-content" not in str(caught.value)
