import json
import math
import shutil
import subprocess
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
RENDERED = {
    "duration": 5.0,
    "width": 404,
    "height": 720,
    "audio": True,
    "audio_sample_rate": 48000,
}
LEDGER = [{"file": "visuals/visual-01.png", "source_credit": "作者", "license": "CC0"}]


def write_ass(folder, events):
    """Writes captions.ass in folder; events are (start, end, text[, style]) in ms."""
    subtitles = pysubs2.SSAFile()
    subtitles.info.update(PlayResX="1080", PlayResY="1920")
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname="Noto Sans CJK JP", fontsize=20, marginl=25, marginr=25
    )
    for start, end, text, *rest in events:
        style = rest[0] if rest else "Default"
        subtitles.append(pysubs2.SSAEvent(start=start, end=end, text=text, style=style))
    subtitles.save(str(folder / "captions.ass"))
    # The SRT holds the same spoken captions, as the pipeline writes it.
    spoken = pysubs2.SSAFile()
    for event in subtitles:
        if event.style in ("Default", "Sequential"):
            spoken.append(
                pysubs2.SSAEvent(start=event.start, end=event.end, text=event.plaintext)
            )
    spoken.save(str(folder / "captions.srt"))
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
        monkeypatch.setattr(
            quality.subprocess, "run", lambda *a, _result=result, **k: _result
        )
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


def test_fast_captions_and_off_target_audio_are_warnings_only(
    tmp_path, monkeypatch, on_target_loudness
):
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


def test_silent_audio_is_a_warning_and_unmeasurable_audio_fails(
    tmp_path, monkeypatch, on_target_loudness
):
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


def test_clip_without_audio_is_not_measured(tmp_path, monkeypatch, on_target_loudness):
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


def test_captions_shown_under_a_second_or_over_four_seconds_are_warnings(
    tmp_path, on_target_loudness
):
    folder = write_ass(tmp_path, [(0, 800, "あ"), (800, 5600, "いいいい")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is True
    assert report["metrics"]["captions_under_1s"] == 1
    assert report["metrics"]["captions_over_4s"] == 1
    assert any("less than 1 second" in text for text in report["warnings"])
    assert any("more than 4 seconds" in text for text in report["warnings"])


def test_a_caption_with_three_lines_fails(tmp_path, on_target_loudness):
    folder = write_ass(tmp_path, [(0, 2000, "一\\N二\\N三")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is False
    assert any("more than 2 lines" in failure for failure in report["failures"])


def test_srt_and_ass_must_carry_the_same_captions(tmp_path, on_target_loudness):
    folder = write_ass(tmp_path, [(0, 2000, "あいう")])
    (folder / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nかきく\n", encoding="utf-8"
    )
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is False
    assert any("differ in timing or text" in failure for failure in report["failures"])


def test_a_video_that_does_not_decode_cleanly_fails(tmp_path, monkeypatch):
    folder = write_ass(tmp_path, [(0, 1000, "あ")])
    monkeypatch.setattr(
        quality,
        "measure_output",
        lambda video: {"integrated_lufs": -16.0, "true_peak_dbtp": -2.0},
    )
    monkeypatch.setattr(
        quality, "decode_errors", lambda video: ["[h264] error while decoding MB"]
    )
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is False
    assert "does not decode cleanly" in report["failures"][0]


def test_audio_that_is_not_48_khz_fails(tmp_path, on_target_loudness):
    folder = write_ass(tmp_path, [(0, 1000, "あ")])
    report = quality.check_clip(
        folder, {**RENDERED, "audio_sample_rate": 44100}, [], []
    )
    assert report["passed"] is False
    assert any("48000 Hz" in failure for failure in report["failures"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_decoding_accepts_a_valid_file_and_reports_a_broken_one(tmp_path):
    good = tmp_path / "good.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(good),
        ],
        check=True,
    )
    assert quality.decode_errors(good) == []
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video stream" * 64)
    assert quality.decode_errors(broken)


def test_a_line_break_inside_a_word_is_a_counted_warning(tmp_path, on_target_loudness):
    folder = write_ass(tmp_path, [(0, 2000, "情報\\N処理")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is True
    assert report["metrics"]["word_splits"] == 1
    assert any("inside a word" in text for text in report["warnings"])


def test_a_caption_break_inside_a_word_is_a_counted_warning(
    tmp_path, on_target_loudness
):
    folder = write_ass(tmp_path, [(0, 1000, "仕事が奪"), (1000, 2000, "われる")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["metrics"]["word_splits"] == 1
    # The text shown is the last three characters before the break and the first three after.
    assert quality.word_splits(quality.caption_events(folder / "captions.ass")) == [
        "事が奪|われる"
    ]


def test_breaks_between_words_and_particles_are_not_flagged(
    tmp_path, on_target_loudness
):
    folder = write_ass(
        tmp_path, [(0, 2000, "仕事が\\N奪われる"), (2000, 4000, "ソフトの\\Nこと")]
    )
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["metrics"]["word_splits"] == 0
    assert not any("inside a word" in text for text in report["warnings"])


def write_framed_ass(folder, events):
    """Like write_ass, with the caption size the pipeline uses at 1080x1920."""
    subtitles = pysubs2.SSAFile()
    subtitles.info.update(PlayResX="1080", PlayResY="1920")
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname="Noto Sans CJK JP", fontsize=92, marginl=25, marginr=25
    )
    for start, end, text in events:
        subtitles.append(pysubs2.SSAEvent(start=start, end=end, text=text))
    subtitles.save(str(folder / "captions.ass"))
    spoken = pysubs2.SSAFile()
    for event in subtitles:
        spoken.append(
            pysubs2.SSAEvent(start=event.start, end=event.end, text=event.plaintext)
        )
    spoken.save(str(folder / "captions.srt"))
    return folder


def test_a_line_wider_than_the_frame_fails_when_measured_again(
    tmp_path, on_target_loudness
):
    # 40 characters at 92 px are 3680 px, far wider than the 1080 px frame.
    folder = write_framed_ass(tmp_path, [(0, 2000, "あ" * 40)])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is False
    assert any("wider than the frame" in text for text in report["failures"])
    assert report["metrics"]["line_overflows"] == 1


def test_a_line_that_fits_the_frame_passes(tmp_path, on_target_loudness):
    folder = write_framed_ass(tmp_path, [(0, 2000, "あ" * 10)])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["metrics"]["line_overflows"] == 0
    assert not any("wider than the frame" in text for text in report["failures"])


def test_a_caption_that_starts_with_a_punctuation_mark_fails(
    tmp_path, on_target_loudness
):
    folder = write_ass(tmp_path, [(0, 2000, "。次の話")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is False
    assert any("starts with a punctuation mark" in text for text in report["failures"])


def test_a_caption_that_starts_with_a_sentence_final_particle_is_a_counted_warning(
    tmp_path, on_target_loudness
):
    folder = write_ass(tmp_path, [(0, 2000, "ね、そうです"), (2000, 4000, "次の話")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is True
    assert report["metrics"]["orphan_particles"] == 1
    assert any("sentence-final particle" in text for text in report["warnings"])


def test_landscape_frames_are_checked_against_four_characters_a_second(
    tmp_path, on_target_loudness
):
    # Ten characters over two seconds read at 5 a second: fine for a portrait frame, too fast here.
    folder = write_ass(tmp_path, [(0, 2000, "あ" * 10)])
    landscape = {**RENDERED, "width": 1920, "height": 1080}
    report = quality.check_clip(folder, landscape, [], [])
    assert report["metrics"]["reading_limit_cps"] == 4
    assert any("faster than 4 characters" in text for text in report["warnings"])
    portrait = quality.check_clip(folder, RENDERED, [], [])
    assert portrait["metrics"]["reading_limit_cps"] == 15
    assert not any("faster than" in text for text in portrait["warnings"])


def test_landscape_reading_and_real_font_width_are_independent(
    tmp_path, on_target_loudness, settings
):
    folder = write_ass(tmp_path, [(0, 2000, "あ" * 10), (2000, 4000, "i" * 20)])
    settings.horizontal_warn_cps = 7
    landscape = {**RENDERED, "width": 1920, "height": 1080}
    report = quality.check_clip(folder, landscape, [], [], settings)
    metrics = report["metrics"]
    assert metrics["reading_target_cps"] == 4
    assert metrics["reading_limit_cps"] == 7
    assert metrics["captions_over_cps"] == {"4": 2, "7": 1, "10": 0}
    assert metrics["reading_count_method"] == "non_whitespace_codepoints"
    assert metrics["lines_over_horizontal_limit"] == 0
    assert metrics["line_max_fullwidth_equivalents"] == 10
    settings.horizontal_line_chars = 8
    narrower = quality.check_clip(folder, landscape, [], [], settings)
    assert narrower["metrics"]["lines_over_horizontal_limit"] == 1
    # Independent 13-equivalent measurement does not silently strengthen the quality gate.
    assert narrower["passed"] == report["passed"]
