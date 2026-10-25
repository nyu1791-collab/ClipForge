import pysubs2
import pytest

from clipforge import caption_breaks, captions, fonts, quality
from clipforge.models import Segment, SelectedClip, Settings, Transcript, Word


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
    assert result["asr_cleanup"] == {
        "repetitions": 0,
        "boilerplate_segments": 0,
        "compression_segments": 0,
    }


def test_caption_cleanup_preserves_input_and_source_time(tmp_path, settings):
    words = [
        Word(start=15 + i * 0.5, end=15.5 + i * 0.5, text="うん") for i in range(4)
    ]
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=10, end=11, text="ご視聴ありがとうございました"),
            Segment(start=11, end=12, text="架空の圧縮判定用の文字列。" * 60),
            Segment(start=15, end=17, text="うん" * 4, words=words),
            Segment(
                start=18,
                end=19,
                text="続き",
                words=[Word(start=18, end=19, text="続き")],
            ),
        ],
    )
    before = transcript.model_dump()
    clip = SelectedClip(
        start=10, end=20, candidate_id="manual", title="t", reason="r", confidence=1
    )
    result = captions.create(transcript, clip, tmp_path, settings)
    srt = pysubs2.load(str(tmp_path / "captions.srt"))
    assert [event.plaintext for event in srt] == ["うんうん", "続き"]
    assert [event.start for event in srt] == [5000, 8000]
    assert result["asr_cleanup"] == {
        "repetitions": 2,
        "boilerplate_segments": 1,
        "compression_segments": 1,
    }
    assert transcript.model_dump() == before


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
    text = "アイウエオカキクケコサシスセソタチツテト"
    words = [
        Word(start=0.1 * i, end=0.1 * i + 0.1, text=text[2 * i : 2 * i + 2])
        for i in range(10)
    ]
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=0, end=1.0, text=text, words=words),
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
    # Two characters at 10 per second need 0.2 s, but no caption is held for less than 1 s
    # when the room allows, so it ends at 3.5 s, before the clip ends.
    assert (second.start, second.end) == (2500, 3500)


class OnePhrase:
    """Stands in for BudouX so the tests do not depend on its phrase model."""

    def parse(self, text):
        return [text]


def test_a_word_start_is_preferred_to_a_break_inside_a_word(monkeypatch):
    monkeypatch.setattr(caption_breaks, "_PARSER", OnePhrase())
    # Six characters fit a line; the break goes before 奪 (a word start), not inside 奪われる.
    assert captions.wrap_breaks("ことは仕事が奪われる", FixedWidth(), 60) == [6]


def test_a_line_never_starts_with_a_closing_mark_or_a_small_kana(monkeypatch):
    monkeypatch.setattr(caption_breaks, "_PARSER", OnePhrase())
    text = "ああああ、いいい"
    # The first line would end before 、 at index 4, so it stops at 3 instead;
    # the second line then breaks after the 、, which is a preferred edge.
    breaks = captions.wrap_breaks(text, FixedWidth(), 40)
    # Dictionary words may move the first break earlier; prohibited starts still never occur.
    assert breaks[-1] == 5
    assert all(text[index] not in captions.NO_LINE_START for index in breaks)
    assert all(text[index] not in captions.NO_LINE_START for index in breaks)


def test_captions_end_at_a_word_boundary_instead_of_inside_a_word(monkeypatch):
    monkeypatch.setattr(caption_breaks, "_PARSER", OnePhrase())
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
    monkeypatch.setattr(caption_breaks, "_PARSER", OnePhrase())
    # The mark at index 3 would start the second line; the break moves in front of 漢 instead.
    assert captions.wrap_breaks("漢字漢́字", FixedWidth(), 30) == [2]


def test_an_english_term_is_not_split_across_lines(monkeypatch):
    monkeypatch.setattr(caption_breaks, "_PARSER", OnePhrase())
    # ChatGPT fits a line whole. The break goes after it, where Latin meets kanji, so that 漢字
    # is not split: 漢|字 is inside a word.
    assert captions.wrap_breaks("漢字ChatGPT漢字", FixedWidth(), 80) == [2, 9]


def test_a_url_is_not_split_across_lines(monkeypatch):
    monkeypatch.setattr(caption_breaks, "_PARSER", OnePhrase())
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
    monkeypatch.setattr(caption_breaks, "_PARSER", OnePhrase())
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


def test_a_short_caption_is_held_to_the_minimum_when_there_is_room():
    # Three characters at 10 a second need only 0.3 s; the floor holds it to 1 s.
    spans = [[0, 300]]
    captions.hold_for_reading(spans, ["あいう"], 9000, floor_ms=1000)
    assert spans == [[0, 1000]]


def test_the_minimum_hold_stops_at_the_next_caption():
    spans = [[0, 300], [600, 900]]
    captions.hold_for_reading(spans, ["あいう", "えお"], 9000, floor_ms=1000)
    assert spans == [[0, 600], [600, 1600]]


def test_landscape_captions_are_held_to_seven_characters_a_second():
    spans = [[0, 1000]]
    cps = Settings.model_fields["horizontal_target_cps"].default
    captions.hold_for_reading(spans, ["あ" * 10], 9000, cps)
    # Ten characters at 7 a second need 1429 ms, held to the next whole centisecond.
    assert spans == [[0, 1430]]


def test_held_caption_ends_stay_on_whole_centiseconds():
    from clipforge.captions import hold_for_reading

    spans = [[1230, 1500], [4560, 4800], [9000, 9100]]
    hold_for_reading(spans, ["あいうえおかきくけこさし", "たちつ", "な"], 20000, cps=7)
    assert all(start % 10 == 0 and end % 10 == 0 for start, end in spans)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert spans[0][1] == 2950  # 12 characters at 7 per second: 1715 ms, rounded up


def test_chat_panel_moves_caption_margins_into_the_video_area(tmp_path, settings):
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
    margin = captions.margin_px(1080)
    for chat_panel in (False, True):
        directory = tmp_path / str(chat_panel)
        directory.mkdir()
        chat = Settings.model_validate(
            {
                **settings.model_dump(),
                "aspect": "16:9",
                "height": 1080,
                "chat_panel": chat_panel,
                "chat_panel_fit": "crop",
            }
        )
        captions.create(transcript, clip, directory, chat)
        subtitles = pysubs2.load(str(directory / "captions.ass"))
        column = 480 if chat_panel else 0
        assert subtitles.styles["Default"].marginl == margin
        assert subtitles.styles["Default"].marginr == margin + column
        limit = 9 if chat_panel else 13
        for event in subtitles:
            for line in event.plaintext.splitlines():
                assert len(line) <= limit


def test_chat_panel_caption_lines_fit_inside_the_video_area(
    tmp_path, settings, japanese_font
):
    chat = Settings.model_validate(
        {
            **settings.model_dump(),
            "aspect": "16:9",
            "height": 1080,
            "chat_panel": True,
            "chat_panel_fit": "crop",
            "horizontal_line_chars": 13,
        }
    )
    assert chat.caption_line_chars() == 9
    text = "長い字幕の行が映像の欄の外へはみ出さないことを確かめます" * 3
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=0, end=6, text=text, words=[Word(start=0, end=6, text=text)])
        ],
    )
    clip = SelectedClip(
        start=0, end=6, candidate_id="manual", title="t", reason="r", confidence=1
    )
    captions.create(transcript, clip, tmp_path, chat)
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    assert subtitles.info["PlayResX"] == "1920"
    assert subtitles.styles["Default"].marginr == captions.margin_px(1080) + 480
    # The quality check measures lines against the margins written in the file.
    assert quality.overflowing_lines(tmp_path / "captions.ass") == []


def test_chat_panel_top_puts_captions_in_the_band_below_the_picture(
    tmp_path, settings, japanese_font
):
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
    chat = Settings.model_validate(
        {
            **settings.model_dump(),
            "aspect": "16:9",
            "height": 1080,
            "chat_panel": True,
            "chat_panel_fit": "top",
        }
    )
    assert chat.chat_panel_fit == "top" and chat.caption_line_chars() == 18
    captions.create(transcript, clip, tmp_path, chat)
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    style = subtitles.styles["Default"]
    assert style.fontsize == 72
    # The band under the 810 px picture is 270 px; two 72 px lines are centred in it.
    assert style.marginv == 63
    assert style.marginl == captions.margin_px(1080)
    assert style.marginr == captions.margin_px(1080) + 480
    # The credit stays on the picture: its bottom edge is 16 px above the picture's bottom.
    for event in subtitles:
        for line in event.plaintext.splitlines():
            assert len(line) <= 18


def test_caption_scale_one_keeps_the_landscape_caption_style(tmp_path, settings):
    text = "横画面の字幕の文字の大きさを確かめます。" * 2
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=0, end=6, text=text, words=[Word(start=0, end=6, text=text)])
        ],
    )
    clip = SelectedClip(
        start=0, end=6, candidate_id="manual", title="t", reason="r", confidence=1
    )
    for scale, size in ((1.0, 52), (1.5, 78)):
        directory = tmp_path / str(scale)
        directory.mkdir()
        landscape = Settings.model_validate(
            {
                **settings.model_dump(),
                "aspect": "16:9",
                "height": 1080,
                "caption_scale": scale,
            }
        )
        captions.create(transcript, clip, directory, landscape)
        style = pysubs2.load(str(directory / "captions.ass")).styles["Default"]
        # Without the chat panel the margins and the bottom placement are as before.
        assert (style.fontsize, style.marginv, style.marginr) == (size, 130, 38)


def _fill_case(texts):
    """Two caption groups, each in its own second of speech, for the chat fill layout."""
    segments = []
    for start, text in zip((0.5, 3.5), texts):
        segments.append(
            Segment(
                start=start,
                end=start + 2,
                text=text,
                words=[Word(start=start, end=start + 2, text=text)],
            )
        )
    transcript = Transcript(language="ja", segments=segments)
    clip = SelectedClip(
        start=0, end=6, candidate_id="manual", title="題", reason="r", confidence=1
    )
    return transcript, clip


def _fill_settings(settings, **updates):
    return Settings.model_validate(
        {
            **settings.model_dump(),
            "aspect": "16:9",
            "height": 1080,
            "chat_panel": True,
            **updates,
        }
    )


def test_fill_captions_sit_bottom_centre_at_their_lifts(
    tmp_path, settings, japanese_font
):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    fill = _fill_settings(settings)
    assert fill.chat_panel_fit == "fill"
    captions.create(transcript, clip, tmp_path, fill, lifts=[0.2, 0.12])
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    lifted, base = [event for event in subtitles if event.style == "Default"]
    # Every fill caption is bottom centre; its own MarginV is its lift in pixels.
    assert lifted.text.startswith(r"{\an2}") and lifted.marginv == round(1080 * 0.2)
    assert base.text.startswith(r"{\an2}") and base.marginv == round(1080 * 0.12)
    assert r"\an8" not in lifted.text + base.text
    style = subtitles.styles["Default"]
    assert style.fontsize == 72 and style.alignment == pysubs2.Alignment.BOTTOM_CENTER
    assert style.marginv == round(1080 * 0.12)
    assert style.marginr == captions.margin_px(1080) + 480
    # Fill thickens the outline in proportion to the 72 px font: 2.5 px at 52 px.
    assert style.outline == 3.5


def test_fill_captions_take_the_base_margin_without_matching_lifts(
    tmp_path, settings, japanese_font
):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    fill = _fill_settings(settings)
    # No list, or one that does not give a lift to every caption: all at the base margin.
    for lifts in (None, [0.2], [0.2, 0.3, 0.1]):
        captions.create(transcript, clip, tmp_path, fill, lifts=lifts)
        subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
        events = [e for e in subtitles if e.style == "Default"]
        assert len(events) == 2
        assert all(e.marginv == round(1080 * 0.12) for e in events)
        assert all(e.text.startswith(r"{\an2}") for e in events)


def test_fill_heading_leaves_the_caption_margins_alone(
    tmp_path, settings, japanese_font
):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    fill = _fill_settings(settings, heading=True)
    captions.create(transcript, clip, tmp_path, fill, lifts=[0.2, 0.2])
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    texts = [e for e in subtitles if e.style == "Default"]
    assert all(e.marginv == round(1080 * 0.2) for e in texts)
    assert subtitles.styles["Heading"].alignment == pysubs2.Alignment.TOP_CENTER


def test_top_fit_ignores_caption_lifts(tmp_path, settings, japanese_font):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    top = _fill_settings(settings, chat_panel_fit="top")
    captions.create(transcript, clip, tmp_path, top, lifts=[0.2, 0.2])
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    events = [e for e in subtitles if e.style == "Default"]
    assert all(r"\an2" not in e.text and e.marginv == 0 for e in events)


def test_event_spans_are_the_times_create_writes(tmp_path, settings, japanese_font):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    fill = _fill_settings(settings)
    spans = captions.event_spans(transcript, clip, fill)
    captions.create(transcript, clip, tmp_path, fill)
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    written = [
        (e.start / 1000, e.end / 1000) for e in subtitles if e.style == "Default"
    ]
    assert spans == written and len(spans) == 2


def test_caption_punctuation_uses_fullwidth_marks_and_one_following_space():
    assert captions.japanese_marks("本当? 次!!　はい!") == "本当？　次！！　はい！"
    assert captions.japanese_marks("本当？　次！！　") == "本当？　次！！　"
    # No space before a closing bracket or another mark.
    assert captions.japanese_marks("えっ!」と?!") == "えっ！」と？！"


def test_dictionary_words_and_noun_no_noun_are_not_line_break_candidates():
    from clipforge.caption_breaks import _unbreakable

    assert {1, 2, 3} <= _unbreakable("大人の人")
    assert {1, 2, 3, 4, 5, 6} <= _unbreakable("マインクラフト")


def test_fast_caption_can_use_nearby_preceding_silence_without_overlap():
    spans = [[0, 1000], [1500, 2000], [2500, 3000]]
    captions.hold_for_reading(
        spans, ["あ" * 7, "い" * 10, "う"], 4000, cps=7, lead_ms=500
    )
    assert 1000 <= spans[1][0] < 1500
    assert spans[1][1] <= spans[2][0]
    assert 10 / ((spans[1][1] - spans[1][0]) / 1000) <= 7


def test_landscape_lifts_place_each_caption_at_its_margin(
    tmp_path, settings, japanese_font
):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    plain = _fill_settings(settings, chat_panel=False)
    captions.create(transcript, clip, tmp_path, plain, lifts=[0.2, 0.25])
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    events = [e for e in subtitles if e.style == "Default"]
    assert [e.marginv for e in events] == [216, 270]
    assert all(r"{\an2}" in e.text for e in events)


def test_landscape_without_lifts_keeps_the_style_margin(
    tmp_path, settings, japanese_font
):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    plain = _fill_settings(settings, chat_panel=False)
    captions.create(transcript, clip, tmp_path, plain)
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    events = [e for e in subtitles if e.style == "Default"]
    assert all(r"\an2" not in e.text and e.marginv == 0 for e in events)


@pytest.mark.parametrize(
    "updates",
    [
        {"chat_panel": False},
        {"chat_panel": True, "chat_panel_fit": "fill"},
        {"chat_panel": True, "chat_panel_fit": "top"},
        {"chat_panel": True, "chat_panel_fit": "crop"},
        {"chat_panel": True, "chat_panel_fit": "letterbox"},
    ],
)
def test_no_landscape_layout_places_captions_at_the_top(
    tmp_path, settings, japanese_font, updates
):
    transcript, clip = _fill_case(("上の字幕です", "下の字幕です"))
    layout = _fill_settings(settings, **updates)
    captions.create(transcript, clip, tmp_path, layout, lifts=[0.2, 0.2])
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    assert subtitles.styles["Default"].alignment == pysubs2.Alignment.BOTTOM_CENTER
    assert all(r"\an8" not in e.text for e in subtitles if e.style == "Default")
    assert r"\an8" not in (tmp_path / "captions.ass").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["plain", "karaoke", "sequential"])
@pytest.mark.parametrize("aspect", ["9:16", "16:9"])
@pytest.mark.parametrize(
    "texts",
    [
        ("本当? これは本当!!", "はい! 次はどう?"),
        (" Hello? next!!　", " Yes! and now?　"),
    ],
)
def test_punctuation_roundtrips_with_identical_ass_and_srt_text(
    tmp_path, settings, japanese_font, mode, aspect, texts
):
    from clipforge import quality

    transcript, clip = _fill_case(texts)
    configured = _fill_settings(settings, chat_panel=False, caption=mode, aspect=aspect)
    captions.create(transcript, clip, tmp_path, configured)
    ass = quality.caption_events(tmp_path / "captions.ass")
    srt = quality.caption_events(tmp_path / "captions.srt")
    assert ass == srt
    text = "".join(event[2].replace("\n", "") for event in ass)
    assert "?" not in text and "!" not in text
    assert "？　" in text or "！　" in text
    assert all(event[2] == event[2].strip() for event in ass)


@pytest.mark.parametrize("mode", ["plain", "karaoke", "sequential"])
@pytest.mark.parametrize("aspect", ["9:16", "16:9", "1:1"])
@pytest.mark.parametrize("repeats", [1, 2])
def test_long_token_fractional_times_roundtrip_in_both_subtitle_formats(
    tmp_path, settings, japanese_font, mode, aspect, repeats
):
    from clipforge import quality

    text = "この日本語字幕は切り抜いた動画の中で正しく表示されます。" * repeats
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(
                start=13.003,
                end=18.997,
                text=text,
                words=[Word(start=13.003, end=18.997, text=text)],
            )
        ],
    )
    clip = SelectedClip(
        start=13, end=19, candidate_id="manual", title="題", reason="r", confidence=1
    )
    configured = _fill_settings(
        settings, chat_panel=False, caption=mode, aspect=aspect, height=720
    )
    captions.create(transcript, clip, tmp_path, configured)
    ass = quality.caption_events(tmp_path / "captions.ass")
    srt = quality.caption_events(tmp_path / "captions.srt")
    assert ass and ass == srt
    if repeats == 2:
        assert len(ass) >= 2
    from clipforge.caption_breaks import NO_LINE_START

    assert all(text.lstrip()[0] not in NO_LINE_START for _, _, text in ass)
    assert all(start % 10 == end % 10 == 0 for start, end, _ in ass)
    assert all(a[1] <= b[0] for a, b in zip(ass, ass[1:]))
    assert captions.event_spans(transcript, clip, configured) == [
        (start / 1000, end / 1000) for start, end, _ in ass
    ]
