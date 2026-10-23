import json
import math
import shutil
import subprocess
from types import SimpleNamespace

import pysubs2
import pytest

from clipforge import caption_sentences, quality
from clipforge.models import Settings
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
    folder = write_ass(tmp_path, [(0, 400, "あ"), (400, 5400, "いいいい")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["passed"] is True
    assert report["metrics"]["captions_under_1s"] == 1
    assert report["metrics"]["captions_over_4s"] == 1
    assert any("less than 0.5 second" in text for text in report["warnings"])
    assert any("more than 4 seconds" in text for text in report["warnings"])


def test_a_caption_of_half_a_second_or_more_is_not_flicker(
    tmp_path, on_target_loudness
):
    folder = write_ass(tmp_path, [(0, 500, "あ"), (500, 2000, "いいいい")])
    report = quality.check_clip(folder, RENDERED, [], [])
    assert report["metrics"]["captions_under_1s"] == 0
    assert not any("less than 0.5 second" in text for text in report["warnings"])


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
    # 情報 is one morpheme, so the break after 情 is inside a word (情報|処理 is a morpheme edge).
    folder = write_ass(tmp_path, [(0, 2000, "情\\N報処理")])
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


def test_a_kanji_break_on_a_phrase_edge_is_not_a_split():
    # BudouX reads 地方|自治体の as two phrases, so a break there is not inside a word.
    assert quality.word_splits([(0, 2000, "地方\n自治体の")]) == []
    split_captions = [(0, 1000, "地方"), (1000, 2000, "自治体の")]
    assert quality.word_splits(split_captions) == []


def test_a_kanji_break_inside_a_phrase_is_still_a_split():
    # 国際連合安全保障 is one phrase, so a break inside it is still reported.
    assert quality.word_splits([(0, 2000, "国際連\n合安全保障")]) == ["国際連|合安全"]
    split_captions = [(0, 1000, "国際連"), (1000, 2000, "合安全保障")]
    assert quality.word_splits(split_captions) == ["国際連|合安全"]


@pytest.mark.parametrize(
    "events",
    [
        [(0, 2000, "すげー村山優人\n俺マイクラやりますね")],
        [(0, 2000, "あの人がこの君\nこの豆を食べる")],
        [(0, 1000, "大人の人"), (1000, 2000, "大人の子")],
    ],
)
def test_breaks_between_morphemes_are_not_splits(events):
    # BudouX reads each of these as one phrase, but fugashi divides 村山|優|人|俺, 君|この
    # and 人|大人, so the break is between words and is not reported.
    assert quality.word_splits(events) == []


def test_a_break_inside_a_morpheme_is_still_a_split():
    # 奪わ is one morpheme, so 奪|われ is inside a word.
    split_captions = [(0, 1000, "彼は奪"), (1000, 2000, "われた")]
    assert quality.word_splits(split_captions) == ["彼は奪|われた"]


def test_without_fugashi_only_the_budoux_edges_count(monkeypatch):
    def no_tagger():
        raise ImportError("fugashi is not installed")

    monkeypatch.setattr(caption_sentences, "_tagger", no_tagger)
    assert quality.word_splits([(0, 2000, "すげー村山優人\n俺マイクラやりますね")]) == [
        "山優人|俺マイ"
    ]
    assert quality.word_splits([(0, 2000, "あの人がこの君\nこの豆を食べる")]) == [
        "この君|この豆"
    ]
    assert quality.word_splits([(0, 1000, "大人の人"), (1000, 2000, "大人の子")]) == [
        "人の人|大人の"
    ]
    # A break on a BudouX phrase edge is still not a split without fugashi.
    assert quality.word_splits([(0, 1000, "地方"), (1000, 2000, "自治体の")]) == []


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


def test_landscape_frames_are_checked_against_seven_characters_a_second(
    tmp_path, on_target_loudness
):
    # Twenty characters over two seconds read at 10 a second: fine for a portrait frame, too fast here.
    folder = write_ass(tmp_path, [(0, 2000, "あ" * 20)])
    landscape = {**RENDERED, "width": 1920, "height": 1080}
    report = quality.check_clip(folder, landscape, [], [])
    assert report["metrics"]["reading_limit_cps"] == 7
    assert any("faster than 7 characters" in text for text in report["warnings"])
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
    assert metrics["reading_target_cps"] == 7
    assert metrics["reading_limit_cps"] == 7
    assert metrics["captions_over_cps"] == {"4": 2, "7": 1, "10": 0}
    assert metrics["reading_count_method"] == "non_whitespace_codepoints"
    assert metrics["lines_over_horizontal_limit"] == 0
    assert metrics["line_max_fullwidth_equivalents"] == 10
    settings.horizontal_line_chars = 8
    narrower = quality.check_clip(folder, landscape, [], [], settings)
    assert narrower["metrics"]["lines_over_horizontal_limit"] == 1
    assert any("caption line(s) exceed 8" in text for text in narrower["warnings"])
    assert not any("caption line(s) exceed" in text for text in report["warnings"])
    # The configured line limit only warns; it does not strengthen the quality gate.
    assert narrower["passed"] == report["passed"]


@pytest.mark.parametrize("line_chars", [8, 20])
def test_japanese_line_limit_uses_settings_and_preserves_halfwidth_counting(
    settings, line_chars
):
    settings.horizontal_line_chars = line_chars
    events = [
        (0, 2000, "あ" * line_chars),
        (2000, 4000, "i" * (2 * line_chars)),
        (4000, 6000, "あ" * line_chars + "i"),
    ]
    warnings = quality.japanese_style_warnings(events, True, settings)
    assert len(warnings) == 1
    assert warnings[0].startswith(
        f"1 caption line(s) exceed {line_chars} fullwidth characters"
    )
    assert quality.japanese_style_warnings(events, False, settings) == []


@pytest.mark.parametrize(
    ("text", "warns"),
    [("あ。", True), ("あ、い、う", True), ("あ　い", False), ("あ．い，う", False)],
)
def test_japanese_full_stops_and_commas_are_counted_without_examples(text, warns):
    warnings = quality.japanese_style_warnings([(0, 2000, text)], landscape=True)
    assert warnings == (
        ["1 caption(s) contain Japanese full stops or commas"] if warns else []
    )


@pytest.mark.parametrize(
    ("text", "warns"),
    [("あ?", True), ("あ!", True), ("あ?!", True), ("あ？！", False), ("あ", False)],
)
def test_halfwidth_question_and_exclamation_marks_are_warnings(text, warns):
    warnings = quality.japanese_style_warnings([(0, 2000, text)], landscape=True)
    assert warnings == (
        [
            "1 caption(s) use halfwidth question or exclamation marks, "
            "use fullwidth marks, for example " + text
        ]
        if warns
        else []
    )


@pytest.mark.parametrize(
    ("text", "warns"),
    [
        ("あ？い", True),
        ("あ！い", True),
        ("あ？ い", True),
        ("あ？！い", True),
        ("あ?い", True),
        ("あ!い", True),
        ("あ？　い", False),
        ("あ！　い", False),
        ("あ？！", False),
        ("あ？\nい", False),
        ("「あ？」", False),
        ("『あ！』", False),
        ("（あ！）", False),
    ],
)
def test_fullwidth_spacing_is_needed_only_before_continuing_text(text, warns):
    warnings = quality.japanese_style_warnings([(0, 2000, text)], landscape=True)
    spacing = [warning for warning in warnings if "lack a fullwidth space" in warning]
    assert spacing == (
        [
            "1 caption(s) lack a fullwidth space after a question or "
            "exclamation mark before more text, for example " + text.replace("\n", " ")
        ]
        if warns
        else []
    )


@pytest.mark.parametrize(
    ("text", "landscape", "count"),
    [
        ("あ" * 14, True, 1),
        ("あ" * 13, True, 0),
        ("i" * 26, True, 0),
        ("i" * 27, True, 1),
        ("ｱ" * 26, True, 0),
        ("ｱ" * 27, True, 1),
        ("あ" * 12 + "ab", True, 0),
        ("あ" * 12 + "abc", True, 1),
        ("あ" * 13 + "\n" + "い" * 13, True, 0),
        ("あ" * 14 + "\n" + "い" * 14, True, 2),
        ("あ" * 14, False, 0),
    ],
)
def test_thirteen_fullwidth_character_limit_counts_lines_and_halfwidths(
    text, landscape, count
):
    warnings = quality.japanese_style_warnings([(0, 2000, text)], landscape)
    assert warnings == (
        [
            f"{count} caption line(s) exceed 13 fullwidth characters, for example "
            + text[:10]
        ]
        if count
        else []
    )


def test_japanese_style_warnings_count_captions_and_truncate_the_first_example():
    first = "あ" * 10 + "、?い！う"
    second = "い、う!え？お。"
    warnings = quality.japanese_style_warnings(
        [(0, 2000, first), (2000, 4000, second)], landscape=True
    )
    assert len(warnings) == 4
    assert warnings[0] == "2 caption(s) contain Japanese full stops or commas"
    assert all(warning.startswith("2 caption(s)") for warning in warnings[1:3])
    assert all(warning.endswith("for example " + "あ" * 10) for warning in warnings[1:])


def test_japanese_style_checks_only_warn_and_do_not_use_font_width(
    tmp_path, on_target_loudness
):
    folder = write_ass(
        tmp_path,
        [
            (0, 3000, "あ、い?う！え。"),
            (3000, 6000, "i" * 27),
            (6000, 9000, "う、!", "Heading"),
        ],
    )
    report = quality.check_clip(
        folder, {**RENDERED, "width": 1920, "height": 1080}, [], []
    )
    assert report["passed"] is True
    assert report["failures"] == []
    assert quality.enforce(report, "strict") is report
    assert report["metrics"]["line_overflows"] == 0
    assert report["metrics"]["lines_over_horizontal_limit"] == 0
    assert "1 caption(s) contain Japanese full stops or commas" in report["warnings"]
    assert any(
        warning.startswith("1 caption(s) use halfwidth")
        for warning in report["warnings"]
    )
    assert any(
        warning.startswith("1 caption(s) lack a fullwidth space")
        for warning in report["warnings"]
    )
    assert any(
        warning.startswith("1 caption line(s) exceed 13")
        for warning in report["warnings"]
    )


def test_top_chat_layout_limits_caption_lines_to_the_picture(settings, japanese_font):
    top = Settings.model_validate(
        {**settings.model_dump(), "aspect": "16:9", "height": 1080, "chat_panel": True}
    )
    events = [(0, 2000, "あ" * 18), (2000, 4000, "あ" * 19)]
    warnings = quality.japanese_style_warnings(events, True, top)
    assert len(warnings) == 1
    assert warnings[0].startswith("1 caption line(s) exceed 18 fullwidth characters")


def test_top_chat_caption_overflow_is_measured_inside_the_picture(
    tmp_path, japanese_font
):
    subtitles = pysubs2.SSAFile()
    subtitles.info.update(PlayResX="1920", PlayResY="1080")
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname="Noto Sans CJK JP", fontsize=72, marginl=38, marginr=518
    )
    # The picture is 1440 px wide; 18 characters at 72 px fit, 19 do not.
    subtitles.append(pysubs2.SSAEvent(start=0, end=1000, text="あ" * 18))
    subtitles.append(pysubs2.SSAEvent(start=1000, end=2000, text="あ" * 19))
    path = tmp_path / "captions.ass"
    subtitles.save(str(path))
    assert quality.overflowing_lines(path) == ["あ" * 19]


def test_fill_chat_layout_limits_caption_lines_to_the_video_area(
    settings, japanese_font
):
    fill = Settings.model_validate(
        {**settings.model_dump(), "aspect": "16:9", "height": 1080, "chat_panel": True}
    )
    assert fill.chat_panel_fit == "fill"
    events = [(0, 2000, "あ" * 18), (2000, 4000, "あ" * 19)]
    warnings = quality.japanese_style_warnings(events, True, fill)
    assert len(warnings) == 1
    assert warnings[0].startswith("1 caption line(s) exceed 18 fullwidth characters")


def test_lifted_caption_is_measured_inside_the_video_area(tmp_path, japanese_font):
    subtitles = pysubs2.SSAFile()
    subtitles.info.update(PlayResX="1920", PlayResY="1080")
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname="Noto Sans CJK JP", fontsize=72, marginl=38, marginr=518
    )
    # A lifted caption is bottom centre with its own MarginV; the width limit is the video area's.
    subtitles.append(
        pysubs2.SSAEvent(start=0, end=1000, text=r"{\an2}" + "あ" * 18, marginv=216)
    )
    subtitles.append(
        pysubs2.SSAEvent(start=1000, end=2000, text=r"{\an2}" + "あ" * 19, marginv=216)
    )
    path = tmp_path / "captions.ass"
    subtitles.save(str(path))
    assert quality.overflowing_lines(path) == ["あ" * 19]


def test_event_margins_replace_the_style_margins_for_the_width_limit(
    tmp_path, japanese_font
):
    subtitles = pysubs2.SSAFile()
    subtitles.info.update(PlayResX="1920", PlayResY="1080")
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname="Noto Sans CJK JP", fontsize=72, marginl=38, marginr=518
    )
    # The first event keeps the style's right margin; the second sets its own, which is wide enough.
    subtitles.append(pysubs2.SSAEvent(start=0, end=1000, text="あ" * 19))
    subtitles.append(
        pysubs2.SSAEvent(start=1000, end=2000, text="あ" * 19, marginr=100)
    )
    path = tmp_path / "captions.ass"
    subtitles.save(str(path))
    assert quality.overflowing_lines(path) == ["あ" * 19]
    subtitles[0].marginr = 100
    subtitles.save(str(path))
    assert quality.overflowing_lines(path) == []


def test_caption_rising_above_the_frame_is_reported(tmp_path, japanese_font):
    subtitles = pysubs2.SSAFile()
    subtitles.info.update(PlayResX="1920", PlayResY="1080")
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname="Noto Sans CJK JP",
        fontsize=72,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginv=130,
        marginl=38,
        marginr=518,
    )
    # Two lines of 72 px at MarginV 1000 reach above the top; MarginV 216 and the style's 130 fit.
    subtitles.append(
        pysubs2.SSAEvent(start=0, end=1000, text=r"{\an2}あ\Nい", marginv=1000)
    )
    subtitles.append(
        pysubs2.SSAEvent(start=1000, end=2000, text=r"{\an2}あ\Nい", marginv=216)
    )
    subtitles.append(pysubs2.SSAEvent(start=2000, end=3000, text="あ\\Nい"))
    path = tmp_path / "captions.ass"
    subtitles.save(str(path))
    assert quality.outside_frame_captions(path) == ["あ\nい"]


def test_frame_check_ignores_files_without_captions(tmp_path):
    path = tmp_path / "captions.ass"
    pysubs2.SSAFile().save(str(path))
    assert quality.outside_frame_captions(path) == []
    assert quality.overflowing_lines(path) == []


def _static_case(tmp_path, first_margin):
    subtitles = pysubs2.SSAFile()
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontsize=52, alignment=pysubs2.Alignment.BOTTOM_CENTER, marginv=130
    )
    for start in (0, 1000):
        subtitles.append(
            pysubs2.SSAEvent(
                start=start,
                end=start + 1000,
                style="Default",
                text=r"{\an2}字幕",
                marginv=first_margin if start == 0 else 130,
            )
        )
    path = tmp_path / "captions.ass"
    subtitles.save(str(path))
    return path


def test_static_overlaps_counts_captions_over_a_band_they_were_placed_by(tmp_path):
    path = _static_case(tmp_path, 346)
    rendered = {"width": 1920, "height": 1080}
    # The first caption's box runs from 346 px (0.32) up about one line; the second sits at 130 px.
    assert quality.static_overlaps(path, [[[0.3, 0.4, "whole"]], []], rendered) == 1
    assert quality.static_overlaps(path, [[[0.5, 0.6, "whole"]], []], rendered) == 0
    assert quality.static_overlaps(path, [[[0.0, 0.1, "left"]], []], rendered) == 0
    # Captions and bands that do not correspond one to one cannot be checked.
    assert quality.static_overlaps(path, [[]], rendered) is None
