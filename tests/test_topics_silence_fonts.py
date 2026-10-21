import shutil
from itertools import pairwise

import pytest

from clipforge import fonts, silence, topics
from clipforge.models import Segment, Transcript, Word
from clipforge.storage import PipelineError


def speech(*pieces):
    """Transcript from (start, end, text) triples; words default to the whole segment."""
    return Transcript(
        language="ja",
        segments=[Segment(start=a, end=b, text=t) for a, b, t in pieces],
    )


def test_topics_split_at_long_pauses_and_cover_the_video():
    transcript = speech(
        (0, 20, "最初の話題です"),
        (22, 40, "同じ話題が続きます"),
        (45, 70, "別の話題に移ります"),
        (70.2, 100, "まだ同じ話題です"),
    )
    spans = topics.split(transcript, 100, min_seconds=5, max_seconds=60)
    assert [round(b, 1) for _, b in spans] == [21.0, 42.5, 100.0]
    assert spans[0][0] == 0.0
    assert all(a[1] == b[0] for a, b in pairwise(spans))


def test_short_topic_gives_up_its_weaker_boundary():
    transcript = speech(
        (0, 20, "最初"), (22, 40, "次"), (45, 70, "最後"), (70.2, 100, "終わり")
    )
    spans = topics.split(transcript, 100, min_seconds=25, max_seconds=60)
    assert len(spans) == 2
    assert all(b - a >= 25 for a, b in spans)


def test_overlong_topic_splits_at_its_strongest_interior_gap():
    # A 0.5 s gap is too short to cut by itself, but it is the only split point available.
    transcript = speech((0, 70, "前半の話です"), (70.5, 140, "後半の話です"))
    spans = topics.split(transcript, 150, min_seconds=10, max_seconds=100)
    assert len(spans) == 2
    assert round(spans[0][1], 2) == 70.25


def test_overlong_topic_without_gaps_is_cut_evenly():
    transcript = speech((0, 300, "一つの長い発話"))
    spans = topics.split(transcript, 300, min_seconds=10, max_seconds=100)
    assert [round(b - a) for a, b in spans] == [100, 100, 100]


def test_topics_without_speech_is_one_span():
    assert topics.split(Transcript(language="ja", segments=[]), 42.5) == [(0.0, 42.5)]


def test_boundary_strengths_are_higher_for_longer_pauses():
    transcript = speech(
        (0, 10, "同じ言葉の話"), (11, 20, "同じ言葉の続き"), (30, 40, "全く別")
    )
    strengths = topics.boundary_strengths(
        sorted(transcript.segments, key=lambda s: s.start)
    )
    assert [index for index, _, _ in strengths] == [0, 1]
    assert strengths[1][2] > strengths[0][2]


def test_silence_spans_use_words_and_clip_to_the_interval():
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(
                start=0,
                end=6,
                text="語",
                words=[
                    Word(start=0, end=2, text="語"),
                    Word(start=4, end=6, text="句"),
                ],
            ),
            Segment(start=8, end=9, text="外", words=[]),
            Segment(start=20, end=21, text="遠い", words=[]),
        ],
    )
    assert silence.transcript_spans(transcript, 1, 8.5) == [(1, 2), (4, 6), (8, 8.5)]


def test_keep_regions_cut_only_long_pauses_and_pad_speech():
    spans = [(0, 2), (2.3, 4), (6, 8)]
    keep = silence.keep_regions(spans, 0, 10, pad=0.1, min_gap=0.5)
    # The 2.0 -> 6.0 pause is cut; the 0.3 s pause between the first two runs is kept.
    assert keep == [(0.0, 4.1), (5.9, 8.1)]
    assert silence.output_duration(keep) == pytest.approx(6.3)


def test_keep_regions_clamp_to_the_interval_and_drop_slivers():
    assert silence.keep_regions([(5.0, 5.01)], 5, 9, pad=0, min_gap=0.4) == []
    assert silence.keep_regions([(0, 1)], 0.5, 3, pad=1, min_gap=0.4) == [(0.5, 2.0)]


def test_output_time_maps_across_cuts_and_snaps_inside_them():
    keep = [(0.0, 4.0), (6.0, 10.0)]
    assert silence.to_output(2.0, keep) == 2.0
    assert silence.to_output(5.0, keep) == 4.0  # inside the cut: snaps to the cut edge
    assert silence.to_output(7.5, keep) == 5.5
    assert silence.to_output(99, keep) == 8.0


def test_interval_pieces_split_where_cuts_remove_audio():
    keep = [(0.0, 4.0), (6.0, 10.0)]
    assert silence.pieces(3.0, 7.0, keep) == [(3.0, 4.0, 0), (4.0, 5.0, 1)]
    assert silence.pieces(4.5, 5.5, keep) == []


def test_font_resolution_refuses_a_missing_family():
    with pytest.raises(PipelineError, match="not installed"):
        fonts.resolve("ClipForge No Such Family 9Q")


def test_font_resolution_needs_fontconfig(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(PipelineError, match="fontconfig"):
        fonts.resolve("Anything")


def test_missing_glyphs_report_only_characters_the_font_lacks(japanese_font):
    assert fonts.missing_glyphs(japanese_font, "あ ") == [""]


def test_measured_width_grows_with_text_and_size(japanese_font):
    small, large = fonts.Measure(japanese_font, 20), fonts.Measure(japanese_font, 40)
    assert small.width("字幕") < small.width("字幕字幕") < large.width("字幕字幕")
