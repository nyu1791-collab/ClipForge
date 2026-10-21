import builtins

import pytest
from pydantic import ValidationError

from clipforge import analysis_budget, selection
from clipforge.models import Candidate, Segment, Settings, Transcript
from clipforge.storage import PipelineError


def candidate(index=1, start=0, end=10):
    return Candidate(
        id=f"candidate-{index:03}",
        start=start,
        end=end,
        heuristic_score=1,
        speech_seconds=0,
        chat_reactions=0,
        scene_boundaries=0,
    )


@pytest.mark.parametrize("value", [0, 1, -0.1, float("inf"), float("nan")])
def test_coverage_configuration_cannot_allow_whole_video(value):
    with pytest.raises(ValidationError):
        Settings(gemini_model="test", max_analysis_coverage=value)


@pytest.mark.parametrize(
    "name",
    [
        "max_analysis_seconds",
        "max_preview_bytes",
        "max_analysis_bytes",
        "max_prompt_text_bytes",
    ],
)
def test_budget_configuration_must_be_positive(name):
    with pytest.raises(ValidationError):
        Settings(gemini_model="test", **{name: 0})


def test_actual_candidates_not_configured_worst_case(settings):
    settings.max_analysis_seconds = 10
    # 4 configured candidates * 10s would not fit, but this actual request fits.
    result = analysis_budget.check_candidates([candidate()], 20, settings)
    assert result["analysis_seconds"] == 10
    assert result["coverage"] == 0.5


def test_overlap_is_counted_twice(settings):
    candidates = [candidate(), candidate(2, start=5, end=15)]
    with pytest.raises(PipelineError, match="max_analysis_coverage"):
        analysis_budget.check_candidates(candidates, 30, settings)
    assert (
        analysis_budget.check_candidates(candidates, 40, settings)["analysis_seconds"]
        == 20
    )


@pytest.mark.parametrize("limit", ["max_analysis_seconds", "max_analysis_coverage"])
def test_exact_seconds_boundary_and_one_microsecond_over(settings, limit):
    if limit == "max_analysis_seconds":
        settings.max_analysis_seconds = 10
        duration = 100
    else:
        duration = 20
    analysis_budget.check_candidates([candidate()], duration, settings)
    if limit == "max_analysis_seconds":
        settings.max_analysis_seconds -= 0.000001
    else:
        duration -= 0.000002
    with pytest.raises(PipelineError, match=limit):
        analysis_budget.check_candidates([candidate()], duration, settings)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_source_duration(settings, duration):
    with pytest.raises(PipelineError, match="source duration"):
        analysis_budget.check_candidates([candidate()], duration, settings)


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        [candidate(i) for i in range(1, 6)],
        [candidate(), candidate()],
        [candidate(end=2)],
        [candidate(end=11)],
        [candidate(start=95, end=105)],
        [candidate().model_copy(update={"start": -1})],
        [candidate().model_copy(update={"end": float("nan")})],
    ],
)
def test_invalid_cached_candidates(settings, candidates):
    with pytest.raises(PipelineError):
        analysis_budget.check_candidates(candidates, 100, settings)


def test_exact_prompt_bytes_count_repeated_japanese_text(settings):
    candidates = [candidate(), candidate(2, start=5, end=15)]
    transcript = Transcript(
        language="ja", segments=[Segment(start=6, end=7, text="日本語")]
    )
    texts, budget = analysis_budget.prepare_text(candidates, transcript, 40, settings)
    assert texts[1].endswith("日本語") and texts[2].endswith("日本語")
    count = sum(len(text.encode("utf-8")) for text in texts)
    assert budget["prompt_text_bytes"] == count
    settings.max_prompt_text_bytes = count
    assert (
        analysis_budget.prepare_text(candidates, transcript, 40, settings)[0] == texts
    )
    settings.max_prompt_text_bytes -= 1
    with pytest.raises(PipelineError, match="max_prompt_text_bytes"):
        analysis_budget.prepare_text(candidates, transcript, 40, settings)


def test_unencodable_text_fails_without_echoing_content(settings):
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=1, end=2, text="valid").model_copy(
                update={"text": "private\ud800"}
            )
        ],
    )
    with pytest.raises(PipelineError, match="valid UTF-8") as caught:
        analysis_budget.prepare_text([candidate()], transcript, 100, settings)
    assert "private" not in str(caught.value)


def test_preview_size_boundaries_with_sparse_files(tmp_path, settings):
    paths = [tmp_path / "one.mp4", tmp_path / "two.mp4"]
    settings.max_preview_bytes = 25 * 1024**2
    settings.max_analysis_bytes = 50 * 1024**2
    for path in paths:
        with path.open("wb") as f:
            f.truncate(settings.max_preview_bytes)
    assert (
        analysis_budget.check_previews(paths, settings)["analysis_bytes"]
        == 50 * 1024**2
    )
    settings.max_analysis_bytes -= 1
    with pytest.raises(PipelineError, match="max_analysis_bytes"):
        analysis_budget.check_previews(paths, settings)
    with paths[-1].open("ab") as f:
        f.write(b"x")
    with pytest.raises(PipelineError, match="max_preview_bytes"):
        analysis_budget.check_previews(paths, settings)


def test_empty_or_missing_preview_is_rejected(tmp_path, settings):
    path = tmp_path / "empty.mp4"
    for create in (False, True):
        if create:
            path.touch()
        with pytest.raises(PipelineError):
            analysis_budget.check_previews([path], settings)


@pytest.mark.parametrize(
    "overflow", ["coverage", "bytes", "total_bytes", "japanese_text", "count"]
)
def test_all_inputs_checked_before_sdk_import(
    tmp_path, monkeypatch, settings, overflow
):
    candidates = [candidate(), candidate(2, start=10, end=20)]
    paths = [tmp_path / "first.mp4", tmp_path / "last.mp4"]
    for path in paths:
        path.write_bytes(b"x")
    transcript = Transcript(language="ja", segments=[])
    duration = 100
    if overflow == "coverage":
        duration = 30
    elif overflow == "bytes":
        with paths[-1].open("wb") as f:
            f.truncate(settings.max_preview_bytes + 1)
    elif overflow == "total_bytes":
        settings.max_analysis_bytes = 1
    elif overflow == "japanese_text":
        transcript.segments = [Segment(start=11, end=12, text="日" * 15000)] * 2
    elif overflow == "count":
        paths.pop()
    real_import = builtins.__import__

    def refuse_sdk(name, *args, **kwargs):
        if name == "google" or name.startswith("google."):
            raise AssertionError("SDK imported before preflight")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_sdk)
    with pytest.raises(PipelineError):
        selection.choose(
            None, paths, candidates, transcript, {"duration": duration}, settings
        )
