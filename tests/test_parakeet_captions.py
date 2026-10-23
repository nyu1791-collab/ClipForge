import json

import pytest

from clipforge import audio_first, parakeet_captions
from clipforge.caption_pack import pack_captions


def chars(text, start=0.0, step=0.1, pause_after=None):
    out, t = [], start
    for index, char in enumerate(text):
        out.append({"char": [char], "start": round(t, 2), "end": round(t + step, 2)})
        t += step + (1.0 if pause_after is not None and index == pause_after else 0)
    return {"char": out}


def test_captions_cut_at_pauses_and_shift_to_source_seconds():
    rows = parakeet_captions.segments(chars("あいうえお", pause_after=1), 100.0, 200.0)
    assert [r["text"] for r in rows] == ["あい", "うえお"]
    assert rows[0]["words"][0] == {"text": "あ", "start": 100.0, "end": 100.1}
    assert rows[1]["start"] == pytest.approx(101.2)


def test_overlong_runs_are_cut_and_empty_characters_dropped():
    stamps = chars("あ" * 70)
    stamps["char"].insert(0, {"char": [""], "start": 0.0, "end": 0.05})
    rows = parakeet_captions.segments(stamps, 0.0, 100.0)
    assert sum(len(r["text"]) for r in rows) == 70
    assert all(len(r["text"]) <= parakeet_captions.LONGEST_CHARS for r in rows)


def test_characters_past_the_clip_end_are_left_out():
    rows = parakeet_captions.segments(chars("あいう"), 10.0, 10.25)
    assert "".join(r["text"] for r in rows) == "あい"


def test_packed_captions_are_read_back_by_the_render(tmp_path):
    (tmp_path / "parakeet-c1.json").write_text(json.dumps({"timestamp": chars("字幕")}))
    review = {
        "analysis_key": "k",
        "clips": [{"candidate_id": "topic-001", "start": 5.0, "end": 9.0}],
    }
    value = parakeet_captions.captions(review, tmp_path)
    (transcript,) = audio_first.caption_overrides(
        {"clips": [{"captions": value["clips"][0]["captions"]}]}, "ja"
    )
    assert transcript.segments[0].text == "字幕"
    assert value["clips"][0] | {"captions": None} == {
        "candidate_id": "topic-001",
        "start": 5.0,
        "end": 9.0,
        "captions": None,
    }
    assert audio_first.pack_captions is pack_captions
