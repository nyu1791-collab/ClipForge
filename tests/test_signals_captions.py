import pysubs2
import pytest

from clipforge import captions, fonts, signals
from clipforge.models import Segment, SelectedClip, Transcript, Word
from clipforge.storage import PipelineError


def test_chat_filtering_offset_reactions_and_privacy(settings):
    settings.chat_offset_seconds = -2
    messages = [
        {"time_in_seconds": 5, "message": "草", "author": "private-user"},
        {"time": 8, "message": "ordinary"},
        {"time": "nan", "message": "lol"},
        {"time": -9},
        {"time": 100},
        None,
        {"time": "bad"},
    ]
    result = signals.chat_signals(30, settings, messages)
    assert result["status"] == "provided"
    assert result["events"] == [
        {"time": 3.0, "weight": 2.0},
        {"time": 6.0, "weight": 1.0},
    ]
    assert "private-user" not in str(result)
    assert "ordinary" not in str(result)


@pytest.mark.parametrize("supplied", [{}, "text", [None] * 100001])
def test_chat_rejects_invalid_supplied_data(settings, supplied):
    with pytest.raises(PipelineError):
        signals.chat_signals(30, settings, supplied)


def test_optional_chat_failure_is_explicit(settings, monkeypatch):
    import chat_downloader

    class Broken:
        def get_chat(self, *args, **kwargs):
            assert kwargs["max_attempts"] == 1
            raise RuntimeError("private provider body")

        def close(self):
            pass

    monkeypatch.setattr(chat_downloader, "ChatDownloader", Broken)
    settings.chat_url = "https://www.youtube.com/watch?v=fixture"
    assert signals.chat_signals(30, settings)["status"] == "unavailable_RuntimeError"


@pytest.mark.parametrize("duration", [3, 5, 10, 30, 3600])
def test_candidates_stay_in_bounds_and_avoid_duplicates(settings, transcript, duration):
    result = signals.candidates(
        {"duration": duration},
        transcript,
        {"boundaries": [5, 10, 15]},
        {"events": []},
        settings,
    )
    assert 1 <= len(result) <= settings.candidates
    assert len({r["id"] for r in result}) == len(result)
    for index, item in enumerate(result):
        assert 0 <= item["start"] < item["end"] <= duration
        assert (
            settings.min_seconds <= item["end"] - item["start"] <= settings.max_seconds
        )
        for previous in result[:index]:
            overlap = max(
                0,
                min(item["end"], previous["end"])
                - max(item["start"], previous["start"]),
            )
            assert (
                overlap
                / min(item["end"] - item["start"], previous["end"] - previous["start"])
                <= 0.45
            )


def test_temporal_diversity_and_chat_peaks(settings):
    empty = Transcript(language="und", segments=[])
    info = {"duration": 600}
    scenes = {"boundaries": []}
    result = signals.candidates(info, empty, scenes, {"events": []}, settings)
    assert any(r["start"] > 300 for r in result)
    chat = {"events": [{"time": 480 + i / 10, "weight": 2} for i in range(30)]}
    result = signals.candidates(info, empty, scenes, chat, settings)
    assert result[0]["start"] <= 480 < result[0]["end"]


def test_short_video_rejected(settings, transcript):
    with pytest.raises(PipelineError):
        signals.candidates(
            {"duration": 1}, transcript, {"boundaries": []}, {"events": []}, settings
        )


def test_no_audio_skips_model_loading(settings):
    assert signals.transcribe(None, {"audio": False}, settings) == {
        "language": "und",
        "segments": [],
    }


@pytest.mark.parametrize("aspect", ["9:16", "16:9", "1:1"])
@pytest.mark.parametrize("height", [720, 1080])
@pytest.mark.parametrize("kind", ["plain", "karaoke"])
def test_subtitle_time_layout_escaping_and_formats(
    tmp_path, settings, aspect, height, kind
):
    settings.aspect, settings.height, settings.caption = aspect, height, kind
    text = (
        "これは日本語の長い字幕を画面の中に収めるための品質確認です。" * 2
        + r"{\pos(0,0)}"
    )
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(
                start=8, end=18, text=text, words=[Word(start=8, end=18, text=text)]
            )
        ],
    )
    clip = SelectedClip(
        start=10, end=16, candidate_id="manual", title="t", reason="r", confidence=1
    )
    result = captions.create(transcript, clip, tmp_path, settings)
    ass = pysubs2.load(str(tmp_path / "captions.ass"))
    srt = pysubs2.load(str(tmp_path / "captions.srt"))
    assert result["events"] == len(ass) > 0
    assert len(srt) == len(ass)
    assert all(0 <= e.start < e.end <= 6000 for e in ass)
    assert all(a.end <= b.start for a, b in zip(ass, ass[1:]))
    assert all("\\pos(" not in e.text for e in ass)
    assert all("\\kf" not in e.text for e in srt)
    assert ("\\kf" in (tmp_path / "captions.ass").read_text()) == (kind == "karaoke")
    measure = fonts.Measure(fonts.resolve(settings.font), round(height * 0.048))
    line_px = (
        int(ass.info["PlayResX"])
        - 2 * captions.margin_px(height)
        - round(captions.OUTLINE_ALLOWANCE * captions.scale(height))
    )
    for event in ass:
        lines = event.plaintext.splitlines()
        assert len(lines) <= 2
        assert all(measure.width(line) <= line_px for line in lines)


def test_empty_captions_valid(tmp_path, settings):
    clip = SelectedClip(
        start=0, end=5, candidate_id="manual", title="t", reason="r", confidence=1
    )
    result = captions.create(
        Transcript(language="und", segments=[]), clip, tmp_path, settings
    )
    assert result["events"] == 0
    assert (tmp_path / "captions.ass").is_file()
    assert (tmp_path / "captions.srt").read_text() == ""


class FixedWidth:
    """Every character is ten pixels wide, so the line break does not depend on a font."""

    def width(self, text):
        return 10 * len(text)


def test_subtitles_prefer_word_boundaries():
    tokens = [(0, 1, "日本語字幕の"), (1, 2, "表示と時刻を")]
    text = captions.group_text(tokens, FixedWidth(), 70, "plain")
    assert text.split(r"\N") == ["日本語字幕の", "表示と時刻を"]


def test_fast_captions_hold_into_silence_and_stop_at_the_next_one():
    spans = [[0, 1000], [2500, 3000], [3100, 3300]]
    texts = ["あ" * 20, "い" * 5, "う" * 30]
    captions.hold_for_reading(spans, texts, 5000)
    # 20 characters take 2 s at the 10-per-second target; the next caption starts at 2.5 s.
    # The last caption is held only up to the clip end.
    assert spans == [[0, 2000], [2500, 3000], [3100, 5000]]


def test_a_caption_within_the_target_keeps_its_timing():
    spans = [[0, 5000]]
    captions.hold_for_reading(spans, ["あ" * 10], 9000)
    assert spans == [[0, 5000]]


def test_fast_speech_stays_on_screen_until_the_next_caption_needs_the_room(
    tmp_path, settings
):
    settings.aspect = "16:9"
    words = [Word(start=0.1 * i, end=0.1 * i + 0.1, text="あい") for i in range(10)]
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=0, end=1.0, text="あい" * 10, words=words),
            Segment(
                start=2.5,
                end=2.6,
                text="うえ",
                words=[Word(start=2.5, end=2.6, text="うえ")],
            ),
        ],
    )
    clip = SelectedClip(
        start=0, end=5, candidate_id="manual", title="t", reason="r", confidence=1
    )
    captions.create(transcript, clip, tmp_path, settings)
    first, second = pysubs2.load(str(tmp_path / "captions.ass"))
    assert (first.start, first.end) == (0, 2000)
    # Two characters at 10 per second need 0.2 s; the speech lasts 0.1 s, so it grows by that.
    assert (second.start, second.end) == (2500, 2700)


class OnePhrase:
    """Stands in for BudouX so the tests do not depend on its phrase model."""

    def parse(self, text):
        return [text]


def test_a_word_start_is_preferred_to_a_break_inside_a_word(monkeypatch):
    monkeypatch.setattr(captions, "_PARSER", OnePhrase())
    # Six characters fit a line; the break goes before 奪 (a word start), not inside 奪われる.
    assert captions.wrap_breaks("ことは仕事が奪われる", FixedWidth(), 60) == [6]


def test_a_line_never_starts_with_a_closing_mark_or_a_small_kana(monkeypatch):
    monkeypatch.setattr(captions, "_PARSER", OnePhrase())
    text = "ああああ、いいい"
    # The first line would end before 、 at index 4, so it stops at 3 instead;
    # the second line then breaks after the 、, which is a preferred edge.
    breaks = captions.wrap_breaks(text, FixedWidth(), 40)
    assert breaks == [3, 5]
    assert all(text[index] not in captions.NO_LINE_START for index in breaks)


def test_captions_end_at_a_word_boundary_instead_of_inside_a_word(monkeypatch):
    monkeypatch.setattr(captions, "_PARSER", OnePhrase())
    tokens = [
        (0.0, 0.2, "仕"),
        (0.2, 0.4, "事"),
        (0.4, 0.6, "が"),
        (0.6, 0.8, "奪"),
        (0.8, 1.0, "わ"),
        (1.0, 1.2, "れる"),
    ]
    groups = captions.group(tokens, FixedWidth(), 30)
    assert [[token[2] for token in g] for g in groups] == [
        ["仕", "事", "が"],
        ["奪", "わ", "れる"],
    ]


def test_latin_terms_that_recognition_writes_in_lower_case_are_capitalized():
    assert (
        captions.normalize_terms("ai には agi と asi、Aiko")
        == "AI には AGI と ASI、Aiko"
    )


def test_a_short_caption_joins_a_neighbour_when_the_result_still_fits():
    long_part = [(0.0, 2.0, "あいうえ")]
    short_part = [(2.0, 2.5, "かき")]
    joined = captions._join_short([long_part, short_part], FixedWidth(), 300, 2)
    assert joined == [long_part + short_part]


def test_a_short_caption_stays_short_when_joining_would_break_a_limit():
    # Joined, the two would last 4.4 s, which is longer than a caption may be.
    groups = [[(0.0, 3.8, "あ")], [(3.8, 4.4, "かき")]]
    assert len(captions._join_short(groups, FixedWidth(), 300, 2)) == 2
    # A pause of one second separates them, so they never join.
    groups = [[(0.0, 2.0, "あいうえ")], [(3.0, 3.5, "かき")]]
    assert len(captions._join_short(groups, FixedWidth(), 300, 2)) == 2


def test_word_cut_avoids_a_fragment_even_at_a_word_start():
    members = [
        (0.0, 0.3, "か"),
        (0.3, 0.6, "父"),  # a word start after か, but the head would be 0.3 s
        (0.6, 2.5, "さんき"),
        (2.5, 3.0, "くけ"),  # a boundary between kana with a head of 2.5 s
    ]
    assert captions._word_cut(members) == 3


def test_margins_scale_with_the_frame_height():
    assert captions.margin_px(720) == captions.MARGIN == 35
    assert captions.margin_px(1920) == 93
    assert captions.scale(1080) == pytest.approx(1.5)
