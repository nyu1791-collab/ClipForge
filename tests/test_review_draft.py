import json

import pytest

from clipforge import topic_hints as th
from clipforge.models import Settings
from clipforge.review_draft import build_review_draft
from clipforge.storage import PipelineError


def topic(topic_id, start, end, *, speech=None):
    return {
        "id": topic_id,
        "start": start,
        "end": end,
        "speech_seconds": end - start if speech is None else speech,
        "text": topic_id,
    }


def inputs(rows):
    analysis = {
        "analysis_key": "synthetic-analysis",
        "duration": 2000,
        "topics": rows,
        "semantic_topic_review": "required",
    }
    previews = {
        "analysis_key": analysis["analysis_key"],
        "previews": [
            {"topic_id": row["id"], "file": row["id"] + ".mp4", "receipt": {}}
            for row in rows
        ],
    }
    settings = Settings(clips=3, min_seconds=20, max_seconds=60)
    return analysis, previews, settings


@pytest.mark.parametrize("count", [3, 4, 5])
def test_default_count_follows_settings_and_touching_intervals_are_allowed(count):
    analysis, previews, settings = inputs(
        [topic(str(n), 300 + n * 40, 340 + n * 40) for n in range(5)]
    )
    settings.clips = count
    draft, diagnostics = build_review_draft(analysis, previews, settings)
    assert len(draft["clips"]) == count
    assert diagnostics["shortfall"] == 0


@pytest.mark.parametrize(
    "fault", ["missing", "failed", "error", "no_file", "no_receipt"]
)
def test_only_successfully_created_preview_ids_are_eligible(fault):
    analysis, previews, settings = inputs(
        [topic("A", 300, 340), topic("B", 400, 440), topic("C", 500, 540)]
    )
    if fault == "missing":
        previews["previews"].pop(0)
    elif fault == "failed":
        previews["previews"][0]["status"] = "failed"
    elif fault == "error":
        previews["previews"][0]["error"] = "synthetic acquisition failure"
    else:
        previews["previews"][0].pop("file" if fault == "no_file" else "receipt")
    draft, diagnostics = build_review_draft(analysis, previews, settings)
    assert [row["candidate_id"] for row in draft["clips"]] == ["B", "C"]
    assert diagnostics["shortfall"] == 1
    assert diagnostics["skipped"] == [
        {"topic_id": "A", "reason": "preview_unavailable"}
    ]


@pytest.mark.parametrize("start, end", [(-1, 39), (1990, 2030), (350, 300)])
def test_intervals_must_be_ordered_and_inside_source(start, end):
    analysis, previews, settings = inputs([topic("A", start, end)])
    draft, diagnostics = build_review_draft(analysis, previews, settings)
    assert draft["clips"] == []
    assert diagnostics["shortfall"] == 3
    assert diagnostics["skipped"] == [{"topic_id": "A", "reason": "invalid_interval"}]


def test_previews_must_belong_to_the_same_analysis():
    analysis, previews, settings = inputs([])
    previews["analysis_key"] = "other-analysis"
    with pytest.raises(PipelineError, match="different audio analysis"):
        build_review_draft(analysis, previews, settings)


def test_volume_bump_reorders_drafts_and_is_recorded_without_text():
    analysis, previews, settings = inputs(
        [topic("A", 300, 340), topic("B", 400, 440), topic("C", 500, 540)]
    )
    levels = [-30.0] * 2000
    for second in range(410, 420):
        levels[second] = -15.0
    volume = [(float(second), level) for second, level in enumerate(levels)]
    draft, diagnostics = build_review_draft(analysis, previews, settings, volume=volume)
    assert [clip["candidate_id"] for clip in draft["clips"]] == ["B", "A", "C"]
    record = diagnostics["volume_hints"]
    assert record["data"] == "available"
    assert record["weight"] == th.VOLUME_WEIGHT
    rows = {row["topic_id"]: row for row in record["topics"]}
    assert rows["B"]["score"] == 1.0 and rows["B"]["bump_db"] == 15.0
    assert rows["A"]["score"] == 0.0 and rows["C"]["score"] == 0.0
    assert set(rows["A"]) == {"topic_id", "bump_db", "score"}
    assert "text" not in json.dumps(record)


def test_without_volume_draft_is_the_text_speech_ranking_and_says_not_available():
    analysis, previews, settings = inputs(
        [topic("A", 300, 340), topic("B", 400, 440), topic("C", 500, 540)]
    )
    draft, diagnostics = build_review_draft(analysis, previews, settings)
    assert [clip["candidate_id"] for clip in draft["clips"]] == ["A", "B", "C"]
    assert diagnostics["volume_hints"] == {
        "data": "not_available",
        "reason": "not_provided",
        "weight": 0.0,
        "topics": [],
    }
    assert build_review_draft(analysis, previews, settings, volume=None) == (
        draft,
        diagnostics,
    )


def test_missing_volume_records_the_reason_without_paths_or_text():
    analysis, previews, settings = inputs(
        [topic("A", 300, 340), topic("B", 400, 440), topic("C", 500, 540)]
    )
    _, diagnostics = build_review_draft(
        analysis, previews, settings, volume_reason="decode_failed"
    )
    assert diagnostics["volume_hints"] == {
        "data": "not_available",
        "reason": "decode_failed",
        "weight": 0.0,
        "topics": [],
    }
