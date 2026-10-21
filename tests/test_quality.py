import json
import math
from types import SimpleNamespace

import pysubs2
import pytest

from clipforge import quality
from clipforge.storage import PipelineError

SUMMARY = """\
[Parsed_ebur128_0 @ 0x55d0] Summary:

  Integrated loudness:
    I:         -16.2 LUFS
    Threshold: -26.2 LUFS

  Loudness range:
    LRA:         4.1 LU
    Threshold: -36.2 LUFS
    LRA low:   -18.5 LUFS
    LRA high:  -14.4 LUFS

  True peak:
    Peak:       -1.4 dBFS
"""
RENDERED = {"duration": 5.0, "width": 404, "height": 720, "audio": True}
LEDGER = [{"file": "visuals/visual-01.png", "source_credit": "作者", "license": "CC0"}]


def write_ass(folder, events):
    """Writes captions.ass in folder; events are (start, end, text[, style]) in ms."""
    subtitles = pysubs2.SSAFile()
    for start, end, text, *rest in events:
        style = rest[0] if rest else "Default"
        subtitles.append(pysubs2.SSAEvent(start=start, end=end, text=text, style=style))
    subtitles.save(str(folder / "captions.ass"))
    return folder


def test_summary_gives_integrated_loudness_and_true_peak():
    assert quality.parse_ebur128_summary(SUMMARY) == {
        "integrated_lufs": -16.2,
        "true_peak_dbtp": -1.4,
    }


def test_silence_measures_negative_infinity_and_no_summary_is_unknown():
    silent = SUMMARY.replace("-16.2", "-inf").replace("-1.4", "-inf")
    parsed = quality.parse_ebur128_summary(silent)
    assert parsed == {"integrated_lufs": -math.inf, "true_peak_dbtp": -math.inf}
    assert quality.parse_ebur128_summary("no summary") == {
        "integrated_lufs": None,
        "true_peak_dbtp": None,
    }


def test_measurement_reads_the_summary_and_refuses_when_it_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        quality.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stderr=SUMMARY),
    )
    assert quality.measure_output(tmp_path / "video.mp4") == {
        "integrated_lufs": -16.2,
        "true_peak_dbtp": -1.4,
    }
    for result in [
        SimpleNamespace(returncode=1, stderr=SUMMARY),
        SimpleNamespace(returncode=0, stderr="no summary"),
    ]:
        monkeypatch.setattr(quality.subprocess, "run", lambda *a, **k: result)
        with pytest.raises(PipelineError, match="could not be measured"):
            quality.measure_output(tmp_path / "video.mp4")


def test_reading_speed_and_overlaps_are_measured_for_spoken_captions_only(tmp_path):
    folder = write_ass(
        tmp_path,
        [
            (0, 1000, "あ" * 15),
            (1000, 2000, "い" * 30),
            (1500, 2500, "う"),
            (3000, 4000, "題名", "Heading"),
        ],
    )
    rates, overlaps = quality.caption_rates(folder / "captions.ass")
    assert rates == [15.0, 30.0, 1.0]
    assert overlaps == 1


def test_a_clean_clip_passes_and_reports_its_metrics(tmp_path, on_target_loudness):
    folder = write_ass(tmp_path, [(0, 1000, "あ" * 10)])
    report = quality.check_clip(folder, RENDERED, [], LEDGER)
    assert report["passed"] is True
    assert report["failures"] == [] and report["warnings"] == []
    assert report["metrics"]["caption_count"] == 1
    assert report["metrics"]["caption_max_cps"] == 10.0
    assert report["metrics"]["integrated_lufs"] == -16.0
    assert report["metrics"]["true_peak_dbtp"] == -2.0


def test_fast_captions_and_off_target_audio_are_warnings_only(tmp_path, monkeypatch):
    folder = write_ass(tmp_path, [(0, 1000, "あ" * 30)])
    monkeypatch.setattr(
        quality,
        "measure_output",
        lambda video: {"integrated_lufs": -11.0, "true_peak_dbtp": 0.5},
    )
    report = quality.check_clip(folder, RENDERED, [], LEDGER)
    assert report["passed"] is True
    assert report["metrics"]["captions_over_limit"] == 1
    assert any("faster than 15" in text for text in report["warnings"])
    assert any("LUFS is outside" in text for text in report["warnings"])
    assert any("true peak" in text for text in report["warnings"])


def test_overlap_missing_glyph_and_unlicensed_visual_are_failures(
    tmp_path, on_target_loudness
):
    folder = write_ass(tmp_path, [(0, 2000, "あ"), (1000, 3000, "い")])
    report = quality.check_clip(folder, RENDERED, ["ー"], [{"file": "visuals/x.png"}])
    assert report["passed"] is False
    assert len(report["failures"]) == 3
    assert "overlap" in report["failures"][0]
    assert "font lacks: ー" in report["failures"][1]
    assert "no source credit or licence" in report["failures"][2]


def test_silent_audio_is_a_warning_and_unmeasurable_audio_fails(tmp_path, monkeypatch):
    folder = write_ass(tmp_path, [])
    monkeypatch.setattr(
        quality,
        "measure_output",
        lambda video: {"integrated_lufs": -math.inf, "true_peak_dbtp": -math.inf},
    )
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is True
    assert report["warnings"] == ["the rendered audio is silent"]
    assert report["metrics"]["integrated_lufs"] is None
    json.dumps(report, allow_nan=False)

    def unreadable(video):
        raise PipelineError("loudness of the rendered clip could not be measured")

    monkeypatch.setattr(quality, "measure_output", unreadable)
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is False
    assert "could not be measured" in report["failures"][0]


def test_clip_without_audio_is_not_measured(tmp_path, monkeypatch):
    folder = write_ass(tmp_path, [(0, 1000, "あ" * 5)])

    def must_not_measure(video):
        raise AssertionError("a clip without audio has no loudness to measure")

    monkeypatch.setattr(quality, "measure_output", must_not_measure)
    report = quality.check_clip(folder, {**RENDERED, "audio": False}, [], [])
    assert report["passed"] is True
    assert report["metrics"]["integrated_lufs"] is None


def test_report_gate_keeps_failures_and_strict_gate_refuses_them():
    failing = {"passed": False, "failures": ["font lacks: ー"], "warnings": []}
    assert quality.enforce(failing, "report") is failing
    with pytest.raises(PipelineError, match="quality gate: font lacks"):
        quality.enforce(failing, "strict")
    passing = {"passed": True, "failures": [], "warnings": []}
    assert quality.enforce(passing, "strict") is passing


def test_record_writes_the_report_beside_the_clip(tmp_path):
    report = {"passed": True, "failures": [], "warnings": [], "metrics": {}}
    assert quality.record(tmp_path, report) is report
    assert json.loads((tmp_path / "quality.json").read_text()) == report
