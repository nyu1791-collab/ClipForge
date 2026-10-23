import json
import subprocess
from pathlib import Path

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
        {"threads": -1},
        {"threads": 1025},
        {"encode_threads": -1},
        {"encode_threads": 1025},
        {"aspect": "4:3"},
        {"height": 1000},
        {"framing": "face-track"},
        {"caption": "html"},
        {"analysis_fps": 0},
        {"analysis_fps": 6},
        {"max_input_bytes": 0},
        {"max_source_seconds": 43201},
        {"max_transport_bytes": 1023},
        {"max_transport_bytes": 16 * 1024**3 + 1},
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


def test_long_source_and_transport_budgets_keep_local_defaults():
    settings = Settings(gemini_model="manual-no-api")
    assert settings.max_source_seconds == 3600
    assert settings.max_input_bytes == 1024**3
    assert settings.max_transport_bytes == 6 * 1024**3
    maximum = Settings(
        gemini_model="manual-no-api",
        max_source_seconds=43200,
        max_transport_bytes=16 * 1024**3,
    )
    assert maximum.max_source_seconds == 43200
    assert maximum.max_transport_bytes == 16 * 1024**3


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


def test_process_failure_names_stage_and_exit_without_arguments(monkeypatch):
    secret_url = "https://media.example/clip.mp4?token=SECRET"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 3, b"", b"private-content"),
    )
    with pytest.raises(PipelineError) as caught:
        process(
            ["/usr/bin/ffmpeg", "-i", secret_url, "--token", "SECRET"],
            stage="preview",
        )
    error = caught.value
    assert str(error) == "ffmpeg failed at stage preview (exit 3)"
    assert (error.stage, error.returncode, error.timed_out) == ("preview", 3, False)
    for private in ("SECRET", "https", "media.example", "--token", "private-content"):
        assert private not in str(error)


def test_process_timeout_is_a_pipeline_error_without_the_command_line(monkeypatch):
    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(
            ["ffprobe", "--token", "SECRET", "https://media.example/?k=SECRET"], 5
        )

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(PipelineError) as caught:
        process(["ffprobe", "--token", "SECRET"], stage="probe")
    error = caught.value
    assert str(error) == "ffprobe timed out at stage probe"
    assert (error.stage, error.returncode, error.timed_out) == ("probe", None, True)
    # The original TimeoutExpired is not chained, since it repeats the full command line.
    assert error.__context__ is None
    assert "SECRET" not in str(error)


def test_failure_fields_are_recorded_only_for_command_failures(tmp_path, monkeypatch):
    from clipforge.pipeline import record_failure
    from clipforge.storage import failure_fields

    assert failure_fields(PipelineError("plain")) == {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, b"", b""),
    )
    with pytest.raises(PipelineError) as caught:
        process(["ffmpeg", "-i", "x"], stage="remux")
    record_failure(tmp_path, caught.value, "render")
    failure = json.loads((tmp_path / "failure.json").read_text())
    assert failure["stage"] == "render"
    assert failure["command_stage"] == "remux"
    assert failure["returncode"] == 1
    assert failure["timed_out"] is False
    assert failure["message"] == "ffmpeg failed at stage remux (exit 1)"


def test_a_pinned_transcript_key_requires_the_saved_transcript(settings):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings.model_validate({**settings.model_dump(), "transcript_key": "a" * 64})
    pinned = settings.model_validate(
        {
            **settings.model_dump(),
            "transcript_key": "a" * 64,
            "transcript_cache": "cache",
            "require_transcript_cache": True,
        }
    )
    assert pinned.transcript_key == "a" * 64


@pytest.mark.parametrize(
    "updates",
    [
        {"chat_panel": True},  # the shared test settings are vertical
        {"chat_panel": True, "aspect": "9:16", "height": 1920, "chat_panel_width": 480},
        {"chat_panel": True, "aspect": "1:1", "height": 1080, "chat_panel_width": 432},
        {"chat_panel": True, "aspect": "16:9", "height": 1080, "silence_cut": True},
        {"chat_panel": True, "aspect": "16:9", "height": 1080, "chat_panel_width": 383},
        {"chat_panel": True, "aspect": "16:9", "height": 1080, "chat_panel_width": 769},
        {"chat_panel": True, "aspect": "16:9", "height": 1080, "chat_panel_width": 481},
        {
            "chat_panel": True,
            "aspect": "16:9",
            "height": 1080,
            "chat_panel_width": 2001,
        },
        {"chat_panel_fit": "stretch"},
    ],
)
def test_chat_panel_rejects_vertical_cut_and_out_of_range_widths(settings, updates):
    with pytest.raises(ValidationError):
        Settings.model_validate({**settings.model_dump(), **updates})


def test_caption_scale_and_the_landscape_chat_config_are_valid(settings):
    landscape = {**settings.model_dump(), "aspect": "16:9", "height": 1080}
    default = Settings.model_validate(landscape)
    assert default.caption_scale == 1.0 and default.caption_font_size() == 52
    scaled = Settings.model_validate({**landscape, "caption_scale": 1.5})
    assert scaled.caption_font_size() == 78
    for value in (0.7, 2.1):
        with pytest.raises(ValidationError):
            Settings.model_validate({**landscape, "caption_scale": value})
    # With the top picture the two lines must fit 80 percent of the band: 108 px at most here.
    large = Settings.model_validate(
        {**landscape, "chat_panel": True, "caption_scale": 2.0}
    )
    assert large.caption_font_size() == 108 and large.caption_line_chars() == 12
    path = Path(__file__).resolve().parents[1] / "config.landscape-chat.example.json"
    example = Settings.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert example.chat_panel is True and example.chat_panel_fit == "top"
    assert example.silence_cut is False and example.aspect == "16:9"


def test_chat_panel_is_off_by_default_and_scales_the_caption_limit(settings):
    landscape = {**settings.model_dump(), "aspect": "16:9", "height": 1080}
    off = Settings.model_validate({**landscape, "horizontal_line_chars": 13})
    assert off.chat_panel is False and off.chat_panel_pixels() == 0
    assert off.caption_line_chars() == 13
    on = Settings.model_validate(
        {**landscape, "chat_panel": True, "horizontal_line_chars": 13}
    )
    assert on.chat_panel_pixels() == 480
    assert on.chat_panel_fit == "top" and on.chat_anonymize is False
    # Top: 1440 px of picture width at the 72 px caption font leaves room for 18 characters.
    assert on.caption_font_size() == 72 and on.caption_line_chars() == 18
    crop = Settings.model_validate({**on.model_dump(), "chat_panel_fit": "crop"})
    # 13 characters of a 1920 px frame become 9 in the 1440 px video area.
    assert crop.caption_line_chars() == 9
    # 720 p landscape: 480 px is 37.5 percent of 1280, inside the allowed range.
    small = Settings.model_validate({**landscape, "height": 720, "chat_panel": True})
    assert small.chat_panel_pixels() == 480
