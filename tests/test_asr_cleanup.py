"""Synthetic ASR output only; no recorded speech or media fixtures."""

import zlib

import pytest

from clipforge.asr_cleanup import clean_segments
from clipforge.models import Segment, Word


def test_empty_segments():
    assert clean_segments([]) == (
        [],
        {"repetitions": 0, "boilerplate_segments": 0, "compression_segments": 0},
    )


@pytest.mark.parametrize(
    ("text", "expected", "removed"),
    [
        ("うん" * 4, "うんうん", 2),
        ("開始。架空語" * 4, "開始。架空語" * 2, 2),
        ("prefix xyxyxyxyxy suffix", "prefix xyxy suffix", 3),
        ("echo echo echo echo tail", "echo echo tail", 2),
        ("x x x x", "x x", 2),
        (
            "longsynthetic longsynthetic longsynthetic longsynthetic",
            "longsynthetic longsynthetic",
            2,
        ),
        ("うんうん、そうそうそうそう。", "うんうん、そうそう。", 2),
    ],
)
def test_repetition_loops_keep_two_occurrences(text, expected, removed):
    segment = Segment(start=10, end=20, text=text)
    before = segment.model_dump()
    result, counts = clean_segments([segment])
    assert result[0].text == expected
    assert (result[0].start, result[0].end) == (10, 20)
    assert counts == {
        "repetitions": removed,
        "boilerplate_segments": 0,
        "compression_segments": 0,
    }
    assert segment.model_dump() == before


@pytest.mark.parametrize(
    "text",
    [
        "架空の箱を青く塗る合成文です。",
        "うんうん",
        "うんうんうん",
        "echo echo",
        "echo echo echo",
        "架空語は一度、架空語は別の位置です。",
        "ご視聴ありがとうございました、という表示を試します。",
        "チャンネル登録のボタンを配置します。",
        "字幕は自動生成されていますが修正できます。",
    ],
)
def test_normal_speech_and_short_repetitions_are_unchanged(text):
    segments = [Segment(start=2, end=8, text=text)]
    result, counts = clean_segments(segments)
    assert result == segments
    assert result is not segments
    assert result[0] is not segments[0]
    assert not any(counts.values())


@pytest.mark.parametrize(
    "text",
    [
        "ご視聴ありがとうございました",
        " チャンネル登録よろしくお願いします！ ",
        "「字幕は自動生成されています」。",
    ],
)
def test_whole_boilerplate_segments_are_removed(text):
    segment = Segment(start=10, end=20, text=text)
    result, counts = clean_segments([segment])
    assert result == []
    assert counts == {
        "repetitions": 0,
        "boilerplate_segments": 1,
        "compression_segments": 0,
    }
    assert segment.text == text


def test_extreme_compression_is_measured_before_repetition_trimming():
    text = "架空の圧縮判定用の文字列。" * 60
    encoded = text.encode("utf-8")
    assert len(zlib.compress(encoded)) / len(encoded) < 0.2
    result, counts = clean_segments([Segment(start=10, end=20, text=text)])
    assert result == []
    assert counts == {
        "repetitions": 0,
        "boilerplate_segments": 0,
        "compression_segments": 1,
    }


@pytest.mark.parametrize("text", ["あ" * 19, "架空の短文です。" * 3])
def test_short_or_moderately_compressible_text_is_not_dropped(text):
    result, counts = clean_segments([Segment(start=10, end=20, text=text)])
    assert len(result) == 1
    assert counts["compression_segments"] == 0


@pytest.mark.parametrize("length", [19, 20])
def test_compression_uses_utf8_bytes_but_length_guard_counts_characters(length):
    text = "🧪" * length
    encoded = text.encode("utf-8")
    assert len(zlib.compress(encoded)) / len(encoded) < 0.2
    result, counts = clean_segments([Segment(start=10, end=20, text=text)])
    assert len(result) == (1 if length == 19 else 0)
    assert counts["compression_segments"] == (0 if length == 19 else 1)


@pytest.mark.parametrize("repeats", [2, 3])
def test_natural_word_repetitions_keep_all_words_and_timestamps(repeats):
    words = [Word(start=10 + i, end=11 + i, text="うん") for i in range(repeats)]
    segment = Segment(start=10, end=20, text="うん" * repeats, words=words)
    result, counts = clean_segments([segment])
    assert result == [segment]
    assert not any(counts.values())


@pytest.mark.parametrize("unit", ["あ", "架空の長い単語例", "echo"])
def test_repeated_words_keep_original_timestamps_and_input(unit):
    words = [Word(start=10 + i, end=11 + i, text=unit) for i in range(4)]
    words.append(Word(start=18, end=19, text="続き"))
    segment = Segment(
        start=10, end=20, text="".join(w.text for w in words), words=words
    )
    before = segment.model_dump()
    result, counts = clean_segments([segment])
    assert result[0].words == words[:2] + words[4:]
    assert result[0].text == unit * 2 + "続き"
    assert (result[0].start, result[0].end) == (10, 20)
    assert counts["repetitions"] == 2
    assert segment.model_dump() == before


def test_removal_inside_a_word_splits_it_without_stretching_the_remaining_time():
    text = "先頭うんうんうんうん末尾"
    word = Word(start=10, end=22, text=text)
    segment = Segment(start=10, end=22, text=text, words=[word])
    result, counts = clean_segments([segment])
    assert result[0].text == "先頭うんうん末尾"
    assert result[0].words == [
        Word(start=10, end=16, text="先頭うんうん"),
        Word(start=20, end=22, text="末尾"),
    ]
    assert counts["repetitions"] == 2
    assert segment.words == [word]
    assert word.text == text


def test_word_alignment_ignores_asr_leading_spaces_only_for_text_projection():
    words = [Word(start=i, end=i + 1, text=" うん") for i in range(4)]
    words.append(Word(start=8, end=9, text=" 続き"))
    segment = Segment(start=0, end=10, text="うんうんうんうん続き", words=words)
    result, counts = clean_segments([segment])
    assert result[0].text == "うんうん続き"
    assert result[0].words == words[:2] + words[4:]
    assert counts["repetitions"] == 2


def test_multiple_runs_and_segment_removals_have_separate_counts():
    segments = [
        Segment(start=0, end=2, text="うん" * 4 + "。" + "はい" * 5),
        Segment(start=3, end=4, text="ご視聴ありがとうございました"),
        Segment(start=5, end=6, text="架空の圧縮判定用の文字列。" * 60),
        Segment(start=9, end=10, text="最後の合成文。"),
    ]
    before = [segment.model_dump() for segment in segments]
    result, counts = clean_segments(segments)
    assert [segment.text for segment in result] == [
        "うんうん。はいはい",
        "最後の合成文。",
    ]
    assert [(segment.start, segment.end) for segment in result] == [(0, 2), (9, 10)]
    assert counts == {
        "repetitions": 5,
        "boilerplate_segments": 1,
        "compression_segments": 1,
    }
    assert [segment.model_dump() for segment in segments] == before
