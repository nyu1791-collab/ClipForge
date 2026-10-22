import pysubs2
import pytest

from clipforge import captions, fonts, signals
from clipforge.models import Segment, SelectedClip, Settings, Transcript, Word
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
    # Portrait frames read at 10 characters a second; landscape frames read at 4 (see the next test).
    settings.aspect = "9:16"
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
    assert captions.margin_px(720) == captions.MARGIN == 25
    assert captions.margin_px(1920) == 67
    assert captions.scale(1080) == pytest.approx(1.5)


def test_a_combining_mark_never_starts_a_line(monkeypatch):
    monkeypatch.setattr(captions, "_PARSER", OnePhrase())
    # The mark at index 3 would start the second line; the break moves in front of 漢 instead.
    assert captions.wrap_breaks("漢字漢́字", FixedWidth(), 30) == [2]


def test_an_english_term_is_not_split_across_lines(monkeypatch):
    monkeypatch.setattr(captions, "_PARSER", OnePhrase())
    # ChatGPT fits a line whole. The break goes after it, where Latin meets kanji, so that 漢字
    # is not split: 漢|字 is inside a word.
    assert captions.wrap_breaks("漢字ChatGPT漢字", FixedWidth(), 80) == [2, 9]


def test_a_url_is_not_split_across_lines(monkeypatch):
    monkeypatch.setattr(captions, "_PARSER", OnePhrase())
    assert (
        captions.wrap_breaks("漢字https://example.com/a漢字", FixedWidth(), 60)[0] == 2
    )


def test_text_is_normalised_to_composed_characters():
    # か followed by a separate voiced mark is the single character が.
    assert captions.safe("が") == "が"


@pytest.mark.parametrize(
    ("prev", "nxt", "rank"),
    [
        ("攻", "撃", 0),  # a kanji compound is one word
        ("本", "当", 0),
        (
            "書",
            "か",
            0,
        ),  # kanji then か is okurigana or a sentence ending, not a particle
        ("解", "い", 0),
        ("。", "次", 3),  # after sentence punctuation
        ("」", "次", 3),  # after a closing mark, which may end a line
        ("次", "」", 0),  # a closing mark never starts a line
        ("手", "が", 2),  # a main particle after kanji
        ("ス", "は", 2),  # a particle after katakana
        ("の", "次", 2),  # a change from hiragana to kanji
        ("ス", "解", 2),  # a change from katakana to kanji
        ("解", "ス", 2),  # a change from kanji to katakana
        ("カ", "タ", 0),  # inside a katakana word
        ("G", "P", 0),  # inside a Latin run
        ("た", "い", 1),  # hiragana with hiragana, when nothing better fits
    ],
)
def test_break_ranks_follow_japanese_word_boundaries(prev, nxt, rank):
    assert captions._boundary_rank(prev, nxt) == rank


@pytest.mark.parametrize(
    ("prev", "nxt", "inside"),
    [
        ("漢", "字", True),  # a kanji compound
        ("ス", "テ", True),  # a katakana run
        ("G", "P", True),  # a Latin run
        ("書", "か", True),  # kanji then okurigana
        ("解", "い", True),
        ("字", "の", False),  # kanji then a main particle
        ("が", "奪", False),  # a particle then kanji
        ("ス", "解", False),  # katakana then kanji is a change of script
        ("た", "い", False),  # hiragana alone never counts as inside a word here
    ],
)
def test_inside_word_marks_only_breaks_that_split_a_word(prev, nxt, inside):
    assert captions.inside_word(prev, nxt) is inside


def test_common_terms_are_written_in_their_usual_spelling():
    assert (
        captions.normalize_terms("chatgpt と openai、gpu と api、youtube")
        == "ChatGPT と OpenAI、GPU と API、YouTube"
    )


def test_a_term_written_as_two_tokens_is_joined_with_its_timing():
    tokens = [
        (0.0, 0.5, "の"),
        (0.5, 1.0, " LM"),
        (1.0, 1.4, " Studio"),
        (1.4, 2.0, "を"),
    ]
    assert captions.join_phrases(tokens) == [
        (0.0, 0.5, "の"),
        (0.5, 1.4, " LM Studio"),
        (1.4, 2.0, "を"),
    ]


def test_a_phrase_is_not_joined_across_punctuation_or_a_partial_match():
    tokens = [(0.0, 0.5, "LM"), (0.5, 1.0, " Studio,"), (1.0, 1.5, " Open")]
    assert captions.join_phrases(tokens) == tokens


def test_a_sentence_end_closes_a_caption_that_has_lasted_a_second():
    tokens = [
        (0.0, 0.6, "今日は"),
        (0.6, 1.5, "晴れです"),
        (1.5, 2.2, "明日は"),
        (2.2, 3.0, "雨です"),
    ]
    groups = captions.group(tokens, FixedWidth(), 300)
    assert [[token[2] for token in group] for group in groups] == [
        ["今日は", "晴れです"],
        ["明日は", "雨です"],
    ]


def test_a_sentence_end_does_not_close_a_caption_shorter_than_a_second():
    tokens = [(0.0, 0.4, "はい"), (0.4, 0.8, "です"), (0.8, 2.0, "次")]
    assert len(captions.group(tokens, FixedWidth(), 300)) == 1


def test_a_sentence_ending_particle_stays_with_the_ending_before_it():
    # です is followed by よ and ね, which are particles, so the sentence goes on until ね.
    tokens = [(0.0, 1.2, "いいです"), (1.2, 1.5, "よね"), (1.5, 2.5, "次")]
    groups = captions.group(tokens, FixedWidth(), 300)
    assert [[token[2] for token in group] for group in groups] == [
        ["いいです", "よね"],
        ["次"],
    ]


def test_a_past_casual_ending_closes_a_caption():
    # The final form of た is read only when a sentence-opening word follows, as it does in speech.
    tokens = [(0.0, 1.2, "昨日は雨だった"), (1.2, 3.0, "そして")]
    groups = captions.group(tokens, FixedWidth(), 300)
    assert [[token[2] for token in group] for group in groups] == [
        ["昨日は雨だった"],
        ["そして"],
    ]
    assert captions.sentence_ends(tokens) == {0}


def test_a_two_line_caption_is_balanced_at_a_word_edge(monkeypatch):
    monkeypatch.setattr(captions, "_PARSER", OnePhrase())
    # A greedy fill would break after 9 characters, leaving 5 on the second line. The break at 7
    # gives two lines of 7 characters, and both are word edges.
    text = "東京都ケーキが漢字ハンバーグ"
    assert captions.wrap_breaks(text, FixedWidth(), 100) == [7]


def test_landscape_captions_keep_to_thirteen_full_width_characters_a_line(
    tmp_path, settings
):
    settings.aspect, settings.height = "16:9", 1080
    text = (
        "これは横画面の字幕の行の長さを確かめるための、少し長い日本語の文章です。" * 2
    )
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=0, end=6, text=text, words=[Word(start=0, end=6, text=text)])
        ],
    )
    clip = SelectedClip(
        start=0, end=6, candidate_id="manual", title="t", reason="r", confidence=1
    )
    captions.create(transcript, clip, tmp_path, settings)
    for event in pysubs2.load(str(tmp_path / "captions.ass")):
        for line in event.plaintext.splitlines():
            assert len(line) <= Settings.model_fields["horizontal_line_chars"].default


def test_landscape_captions_are_held_to_four_characters_a_second():
    spans = [[0, 1000]]
    cps = Settings.model_fields["horizontal_target_cps"].default
    captions.hold_for_reading(spans, ["あ" * 10], 9000, cps)
    # Ten characters at 4 a second need 2.5 s, so the caption is held to 2.5 s.
    assert spans == [[0, 2500]]
