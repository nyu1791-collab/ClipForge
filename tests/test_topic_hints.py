import math
import json

import pytest

from clipforge import topic_hints as th

SECRET = "秘密の本文マーカーXYZ"


def topic(id_, start, end, speech, text="通常の話題です。"):
    return {
        "id": id_,
        "start": start,
        "end": end,
        "speech_seconds": speech,
        "text": text,
    }


def hint_for(row, duration=1000):
    analysis = {"duration": duration, "topics": [row]}
    return th.hint_topics(analysis)["topics"][0]


def test_edge_start_and_end_positive_and_negative():
    assert hint_for(topic("a", 0, 60, 30))["edge"] is True
    assert hint_for(topic("a", 120, 180, 30))["edge"] is True
    assert hint_for(topic("a", 300, 360, 30))["edge"] is False
    assert hint_for(topic("a", 820, 880, 30))["edge"] is True
    assert hint_for(topic("a", 700, 819, 30))["edge"] is False


def test_a_topic_that_starts_before_the_tail_and_ends_in_it_is_an_edge():
    # Duration 1000 s: the tail starts at 820 s. The topic starts at 700 s but ends at 850 s.
    assert hint_for(topic("a", 700, 850, 30))["edge"] is True
    assert hint_for(topic("a", 700, 760, 30))["edge"] is False


def test_edge_uses_max_end_when_duration_missing():
    analysis = {"topics": [topic("a", 0, 50, 10), topic("b", 400, 500, 10)]}
    rows = {r["id"]: r for r in th.hint_topics(analysis)["topics"]}
    assert rows["a"]["edge"] is True
    assert rows["b"]["edge"] is True


def test_greeting_needs_two_phrase_hits():
    assert hint_for(topic("a", 300, 360, 30, "おはよう、おはよう！"))["greeting"]
    assert hint_for(topic("a", 300, 360, 30, "初見さんいらっしゃい。また明日"))[
        "greeting"
    ]
    assert not hint_for(topic("a", 300, 360, 30, "こんにちは。"))["greeting"]
    assert not hint_for(topic("a", 300, 360, 30, "関係ない話です。"))["greeting"]


def test_repetition_positive_and_negative():
    positive = hint_for(topic("a", 300, 360, 30, "はっ" * 10))
    assert positive["repetition"] is True
    assert positive["repeat_max"] == 10
    assert hint_for(topic("a", 300, 360, 30, "あ" * 12))["repetition"] is True
    assert hint_for(topic("a", 300, 360, 30, "はっ" * 9))["repetition"] is False
    assert hint_for(topic("a", 300, 360, 30, "はっ " * 5 + "x"))["repetition"] is False


def test_density_ratio_and_chars_per_second():
    row = hint_for(topic("a", 300, 360, 30, "あ" * 120))
    assert row["speech_ratio"] == 0.5
    assert row["chars_per_second"] == 2.0


def test_flags_lower_score_and_sort_order():
    plain = topic("plain", 300, 360, 30, "あ" * 240)
    greeted = topic("greet", 300, 360, 30, "おはよう。おはよう。" + "あ" * 240)
    analysis = {"duration": 1000, "topics": [greeted, plain]}
    rows = th.hint_topics(analysis)["topics"]
    assert [r["id"] for r in rows] == ["plain", "greet"]
    assert rows[0]["score"] > rows[1]["score"]


@pytest.mark.parametrize(
    ("speech_a", "speech_b", "expected_duplicate"),
    [(45, 30, "b"), (30, 45, "a"), (30, 30, "b"), (30, 30.0001, "a")],
)
def test_near_duplicates_mark_both_but_penalize_only_lower_density_or_later(
    speech_a, speech_b, expected_duplicate
):
    topics = [
        topic("a", 300, 360, speech_a, "架空の青いロボットが庭で花を育てます"),
        topic("b", 400, 460, speech_b, "架空の青いロボットが庭で花を育てますよ"),
    ]
    # Reverse input order: a density tie must still prefer the earlier topic.
    result = th.hint_topics({"duration": 1000, "topics": topics[::-1]})
    rows = {row["id"]: row for row in result["topics"]}
    for original in topics:
        row = rows[original["id"]]
        assert row["near_duplicates"] == ["b" if row["id"] == "a" else "a"]
        assert th.NEAR_DUPLICATE_THRESHOLD <= row["max_similarity"] < 1
        assert row["max_similarity"] == round(row["max_similarity"], 3)
        assert row["duplicate"] is (row["id"] == expected_duplicate)
        penalty = th.PENALTY["duplicate"] if row["duplicate"] else 0
        assert row["score"] == pytest.approx(hint_for(original)["score"] - penalty)
        assert original["text"] not in json.dumps(result, ensure_ascii=False)


def test_near_duplicates_normalize_nfkc_whitespace_punctuation_and_symbols():
    topics = [
        topic("a", 300, 360, 30, "ＡＢＣ　架空の、庭！＋★\n花"),
        topic("b", 400, 460, 30, "ABC架空の庭花"),
    ]
    rows = th.hint_topics({"duration": 1000, "topics": topics})["topics"]
    assert all(row["near_duplicates"] for row in rows)
    assert all(row["max_similarity"] == 1.0 for row in rows)


@pytest.mark.parametrize(
    ("left", "right", "similarity"),
    [
        ("架空の青いロボット", "月面探検と宇宙船", 0.0),
        ("abcdef", "abcxyz", 0.143),
        ("！★", "\n　", 0.0),
        ("あい", "あい", 0.0),
    ],
)
def test_unrelated_or_short_topics_have_no_duplicate_penalty(left, right, similarity):
    topics = [topic("a", 300, 360, 30, left), topic("b", 400, 460, 30, right)]
    rows = th.hint_topics({"duration": 1000, "topics": topics})["topics"]
    for row in rows:
        assert row["near_duplicates"] == []
        assert row["max_similarity"] == similarity
        assert row["duplicate"] is False
        original = next(original for original in topics if original["id"] == row["id"])
        assert row["score"] == hint_for(original)["score"]


def test_near_duplicates_rank_top_three_and_penalize_only_once():
    topics = [
        topic("a", 300, 360, 10, "abcdefghij"),
        topic("b", 400, 460, 30, "abcdefghijXYZ"),
        topic("c", 500, 560, 30, "abcdefghijXY"),
        topic("d", 600, 660, 30, "abcdefghijX"),
        topic("e", 700, 760, 30, "abcdefghij"),
    ]
    rows = th.hint_topics({"duration": 1000, "topics": topics})["topics"]
    row = next(row for row in rows if row["id"] == "a")
    assert row["near_duplicates"] == ["e", "d", "c"]
    assert row["max_similarity"] == 1.0
    assert row["duplicate"] is True
    assert row["score"] == pytest.approx(
        hint_for(topics[0])["score"] - th.PENALTY["duplicate"]
    )
    assert len(rows) == len(topics)


def test_near_duplicate_threshold_is_inclusive():
    topics = [
        topic("a", 300, 360, 30, "abcde"),
        topic("b", 400, 460, 30, "abcdefgh"),
    ]
    # Three shared trigrams out of six distinct trigrams: exactly 0.5.
    rows = th.hint_topics({"duration": 1000, "topics": topics})["topics"]
    assert all(row["max_similarity"] == 0.5 for row in rows)
    assert all(row["near_duplicates"] for row in rows)


def test_output_never_contains_body_text():
    text = SECRET * 3 + "おはよう" * 2 + "はっ" * 10
    analysis = {"duration": 1000, "topics": [topic("a", 0, 60, 30, text)]}
    result = th.hint_topics(analysis)
    dumped = json.dumps(result, ensure_ascii=False)
    assert SECRET not in dumped
    assert "text" not in result["topics"][0]


def test_invalid_input_is_rejected_without_echoing_body():
    with pytest.raises(ValueError):
        th.hint_topics({"topics": "nope"})
    with pytest.raises(ValueError):
        th.hint_topics({"topics": [{"id": "a", "start": 1, "end": 2, "text": SECRET}]})


def volume_series(levels, start=0.0):
    return [(start + index, level) for index, level in enumerate(levels)]


def quiet_with_bump(bump_at, bump_db=15.0, length=800, base=-30.0, width=10):
    levels = [base] * length
    for index in range(bump_at, bump_at + width):
        levels[index] = base + bump_db
    return volume_series(levels)


PLAIN_TEXT = "一日の終わりに少し話します。"
OTHER_TEXT = "明日の予定は特にありません。"


def test_without_volume_output_matches_text_and_speech_ranking():
    rows = [topic("a", 300, 360, 30, PLAIN_TEXT), topic("b", 400, 460, 10, OTHER_TEXT)]
    analysis = {"duration": 1000, "topics": rows}
    plain = th.hint_topics(analysis)
    assert th.hint_topics(analysis, volume=None) == plain
    assert "volume_weight" not in plain
    assert all("volume_score" not in row for row in plain["topics"])
    assert all("volume_bump_db" not in row for row in plain["topics"])
    expected = round(0.5 * 0.5 + 0.5 * (14 / 60 / th.CHARS_PER_SECOND_REFERENCE), 4)
    assert hint_for(topic("a", 300, 360, 30, PLAIN_TEXT))["score"] == expected


def test_bump_inside_topic_raises_its_rank_by_at_most_the_weight():
    quiet = topic("quiet", 300, 360, 30, PLAIN_TEXT)
    loud = topic("loud", 500, 560, 30, OTHER_TEXT)
    analysis = {"duration": 1000, "topics": [quiet, loud]}
    plain = {r["id"]: r for r in th.hint_topics(analysis)["topics"]}
    rows = th.hint_topics(analysis, volume=quiet_with_bump(520))["topics"]
    assert [r["id"] for r in rows] == ["loud", "quiet"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["loud"]["volume_bump_db"] == 15.0
    assert by_id["loud"]["volume_score"] == 1.0
    assert by_id["quiet"]["volume_score"] == 0.0
    gain = by_id["loud"]["score"] - plain["loud"]["score"]
    assert gain == pytest.approx(th.VOLUME_WEIGHT, abs=1e-4)


def test_bumps_just_outside_the_topic_are_not_counted():
    row = topic("a", 300, 360, 30, PLAIN_TEXT)
    before = quiet_with_bump(290)
    after = quiet_with_bump(360)
    for volume in (before, after):
        hint = th.hint_topics({"duration": 1000, "topics": [row]}, volume=volume)
        assert hint["topics"][0]["volume_score"] == 0.0
        assert hint["topics"][0]["volume_bump_db"] == 0.0


def test_bump_straddling_the_topic_start_counts_its_inside_part():
    row = topic("a", 300, 360, 30, PLAIN_TEXT)
    hint = th.hint_topics(
        {"duration": 1000, "topics": [row]}, volume=quiet_with_bump(299)
    )
    assert hint["topics"][0]["volume_score"] == 1.0


def test_bump_score_scales_between_minimum_and_full_dB():
    row = topic("a", 300, 360, 30, PLAIN_TEXT)
    analysis = {"duration": 1000, "topics": [row]}
    for bump_db, expected in [
        (3.0, 0.0),
        (6.0, 0.0),
        (10.5, 0.5),
        (15.0, 1.0),
        (20.0, 1.0),
    ]:
        hint = th.hint_topics(analysis, volume=quiet_with_bump(320, bump_db))
        assert hint["topics"][0]["volume_score"] == pytest.approx(expected)


def test_local_median_makes_a_uniformly_loud_stream_flat():
    row = topic("a", 300, 360, 30, PLAIN_TEXT)
    volume = volume_series([-10.0] * 800)
    hint = th.hint_topics({"duration": 1000, "topics": [row]}, volume=volume)
    assert hint["topics"][0]["volume_bump_db"] == 0.0
    assert hint["topics"][0]["volume_score"] == 0.0


def test_volume_never_overturns_a_clear_speech_or_text_difference():
    dense = topic("dense", 300, 360, 60, PLAIN_TEXT)
    sparse = topic("sparse", 500, 560, 0, PLAIN_TEXT)
    volume = quiet_with_bump(520, bump_db=40.0)
    rows = th.hint_topics({"duration": 1000, "topics": [sparse, dense]}, volume=volume)
    assert [r["id"] for r in rows["topics"]] == ["dense", "sparse"]
    assert rows["topics"][1]["score"] < rows["topics"][0]["score"]
    repeated = topic("repeat", 500, 560, 30, "はっ" * 12)
    rows = th.hint_topics(
        {
            "duration": 1000,
            "topics": [repeated, topic("plain", 300, 360, 30, OTHER_TEXT)],
        },
        volume=quiet_with_bump(520, bump_db=40.0),
    )
    assert [r["id"] for r in rows["topics"]] == ["plain", "repeat"]


@pytest.mark.parametrize(
    "volume",
    [
        "not a list",
        [(0, -30.0), (2, -30.0)],
        [(0, "loud")],
        [(0, -30.0, 1)],
        [(0, float("nan"))],
    ],
)
def test_invalid_volume_series_is_rejected(volume):
    row = topic("a", 300, 360, 30, PLAIN_TEXT)
    with pytest.raises(ValueError, match="volume|invalid"):
        th.hint_topics({"duration": 1000, "topics": [row]}, volume=volume)


def silence_then_speech(silence=120, speech=120, speech_db=-30.0, wander=0.0):
    levels = [-90.0] * silence
    levels += [speech_db + wander * math.sin(index) for index in range(speech)]
    return volume_series(levels)


def test_speech_starting_after_long_silence_is_not_a_bump():
    # Silence is floored at -90 dB; the first speech windows must not look like a 60 dB peak.
    row = topic("a", 120, 180, 30, PLAIN_TEXT)
    volume = silence_then_speech(wander=2.0)
    hint = th.hint_topics({"duration": 1000, "topics": [row]}, volume=volume)
    assert hint["topics"][0]["volume_score"] == 0.0
    assert hint["topics"][0]["volume_bump_db"] < th.VOLUME_MIN_BUMP_DB
    straddling = topic("b", 110, 170, 30, PLAIN_TEXT)
    hint = th.hint_topics({"duration": 1000, "topics": [straddling]}, volume=volume)
    assert hint["topics"][0]["volume_score"] == 0.0


def test_topic_inside_silence_has_no_bump():
    row = topic("a", 10, 70, 30, PLAIN_TEXT)
    hint = th.hint_topics(
        {"duration": 1000, "topics": [row]}, volume=silence_then_speech()
    )
    assert hint["topics"][0]["volume_bump_db"] == 0.0
    assert hint["topics"][0]["volume_score"] == 0.0


def test_ordinary_four_dB_wander_scores_nothing():
    row = topic("a", 300, 360, 30, PLAIN_TEXT)
    volume = silence_then_speech(silence=0, speech=800, wander=4.0)
    hint = th.hint_topics({"duration": 1000, "topics": [row]}, volume=volume)
    assert hint["topics"][0]["volume_bump_db"] < th.VOLUME_MIN_BUMP_DB
    assert hint["topics"][0]["volume_score"] == 0.0


def test_one_loud_window_alone_does_not_reach_full_score():
    levels = [-30.0] * 800
    levels[320] = 0.0  # one 30 dB spike inside the topic
    row = topic("a", 300, 360, 30, PLAIN_TEXT)
    hint = th.hint_topics(
        {"duration": 1000, "topics": [row]}, volume=volume_series(levels)
    )
    assert hint["topics"][0]["volume_bump_db"] == pytest.approx(10.0)
    assert 0.0 < hint["topics"][0]["volume_score"] < 1.0


def test_chat_bump_is_bounded_text_free_and_only_uses_observed_windows():
    analysis = {
        "duration": 1000,
        "topics": [topic("a", 300, 310, 5), topic("b", 700, 710, 5, "別の話題です")],
    }
    chat = [(i, 9 if i in (304, 305, 306) else 0) for i in range(300, 310)]
    base = {x["id"]: x for x in th.hint_topics(analysis)["topics"]}
    hints = {x["id"]: x for x in th.hint_topics(analysis, chat=chat)["topics"]}
    assert hints["a"]["score"] - base["a"]["score"] == pytest.approx(th.CHAT_WEIGHT)
    assert hints["a"]["chat_messages"] == 27
    assert hints["b"]["chat_score"] is None
    assert hints["b"]["score"] == base["b"]["score"]
    assert "text" not in hints["a"]
    assert th.hint_topics(analysis, chat=None) == th.hint_topics(analysis)


@pytest.mark.parametrize(
    "chat", [[(0, -1)], [(0, True)], [(0, 1.2)], [(0, 1), (0, 2)], [(float("nan"), 1)]]
)
def test_chat_windows_reject_invalid_counts_and_overlapping_samples(chat):
    with pytest.raises(ValueError):
        th.hint_topics({"duration": 100, "topics": []}, chat=chat)
