import json
from pathlib import Path
import subprocess
import sys

import pytest

from clipforge import topic_hints as th

TOOL = Path(__file__).resolve().parents[1] / "tools" / "topic_hints.py"
SECRET = "秘密の本文マーカーXYZ"


def topic(id_, start, end, speech, text="通常の話題です。"):
    return {
        "id": id_,
        "start": start,
        "end": end,
        "speech_seconds": speech,
        "text": text,
    }


def hint_for(row, duration=1000, terms=None):
    analysis = {"duration": duration, "topics": [row]}
    return th.hint_topics(analysis, profanity_terms=terms)["topics"][0]


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


def test_profanity_not_judged_by_default_and_judged_with_terms():
    text = "禁止語A を含む"
    assert hint_for(topic("a", 300, 360, 30, text))["profanity"] is None
    assert hint_for(topic("a", 300, 360, 30, text), terms=["禁止語A"])["profanity"]
    assert not hint_for(topic("a", 300, 360, 30, text), terms=["別語"])["profanity"]


def test_output_never_contains_body_text():
    text = SECRET * 3 + "おはよう" * 2 + "はっ" * 10
    analysis = {"duration": 1000, "topics": [topic("a", 0, 60, 30, text)]}
    result = th.hint_topics(analysis, profanity_terms=[SECRET])
    dumped = json.dumps(result, ensure_ascii=False)
    assert SECRET not in dumped
    assert "text" not in result["topics"][0]


def test_invalid_input_is_rejected_without_echoing_body():
    with pytest.raises(ValueError):
        th.hint_topics({"topics": "nope"})
    with pytest.raises(ValueError):
        th.hint_topics({"topics": [{"id": "a", "start": 1, "end": 2, "text": SECRET}]})


def test_cli_outputs_json_without_body(tmp_path):
    source = tmp_path / "topics.json"
    analysis = {
        "duration": 1000,
        "topics": [topic("topic-001", 0, 60, 30, SECRET + "おはよう" * 2)],
    }
    source.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(TOOL), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert SECRET not in completed.stdout
    assert SECRET not in completed.stderr
    data = json.loads(completed.stdout)
    assert data["topics"][0]["id"] == "topic-001"
    assert data["topics"][0]["greeting"] is True


def test_cli_profanity_dictionary_and_failure(tmp_path, capsys):
    source = tmp_path / "topics.json"
    source.write_text(
        json.dumps({"duration": 1000, "topics": [topic("a", 0, 60, 30, SECRET)]}),
        encoding="utf-8",
    )
    terms = tmp_path / "terms.txt"
    terms.write_text(f"\n{SECRET}\n", encoding="utf-8")
    assert th.main([str(source), "--profanity-dict", str(terms)]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert json.loads(out)["topics"][0]["profanity"] is True

    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    assert th.main([str(broken)]) == 1
    assert SECRET not in capsys.readouterr().err
