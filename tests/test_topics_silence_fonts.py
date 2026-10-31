import shutil
from itertools import pairwise

import pytest

from clipforge import fonts, topics
from clipforge.models import Segment, Transcript
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


def test_topics_without_speech_is_one_span():
    assert topics.split(Transcript(language="ja", segments=[]), 42.5) == [(0.0, 42.5)]


def test_boundary_strengths_are_higher_for_longer_pauses():
    transcript = speech(
        (0, 10, "同じ言葉の話"), (11, 20, "同じ言葉の続き"), (30, 40, "全く別")
    )
    strengths = topics.boundary_strengths(
        sorted(transcript.segments, key=lambda s: s.start)
    )
    assert [index for index, _, _, _ in strengths] == [0, 1]
    assert strengths[1][2] > strengths[0][2]


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


def test_a_segment_nested_in_a_longer_one_does_not_shift_the_cut():
    transcript = speech((0, 10, "長い発話"), (5, 6, "内側"), (12, 20, "次の発話"))
    spans = topics.split(transcript, 20, min_seconds=1, max_seconds=100)
    assert [round(b, 1) for _, b in spans] == [11.0, 20.0]
