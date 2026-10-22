import json

import pytest

from clipforge import audio_first
from clipforge import topic_hints as th
from clipforge.models import Settings, Transcript
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
    settings = Settings(
        gemini_model="manual-no-api", clips=3, min_seconds=20, max_seconds=60
    )
    return analysis, previews, settings


def test_ranked_draft_uses_original_intervals_and_is_never_approved():
    analysis, previews, settings = inputs(
        [
            topic("A", 300, 340, speech=1),
            topic("B", 400, 440),
            topic("C", 500, 540, speech=30),
            topic("D", 600, 640, speech=20),
        ]
    )
    draft, diagnostics = build_review_draft(analysis, previews, settings)
    assert draft == {
        "analysis_key": analysis["analysis_key"],
        "clips": [
            {
                "candidate_id": topic_id,
                "start": start,
                "end": start + 40,
                "title": "要確認：" + topic_id,
                "reason": "ヒント順位に基づく下書き。意味・映像は未確認。",
                "confidence": 0.0,
                "video_reviewed": False,
            }
            for topic_id, start in [("B", 400), ("C", 500), ("D", 600)]
        ],
    }
    assert diagnostics["shortfall"] == 0
    assert diagnostics["transcript_boundary_check"] == "not_available"
    assert diagnostics["status"] == "awaiting_video_review"
    assert diagnostics["semantic_topic_review"] == "required"
    assert diagnostics["human_review_performed"] is False
    assert analysis["semantic_topic_review"] == "required"
    with pytest.raises(PipelineError, match="requires video review"):
        audio_first.reviewed_clips(draft, analysis, previews, settings)
    for clip in draft["clips"]:
        clip["video_reviewed"] = True
    assert len(audio_first.reviewed_clips(draft, analysis, previews, settings)) == 3


def test_distinct_nonoverlapping_ids_duration_limits_and_shortfall():
    analysis, previews, settings = inputs(
        [
            topic("A", 300, 340),
            topic("A", 400, 440),
            topic("B", 330, 370),
            topic("C", 500, 520, speech=16),
            topic("D", 600, 660, speech=42),
            topic("E", 700, 719),
            topic("F", 800, 861),
        ]
    )
    draft, diagnostics = build_review_draft(analysis, previews, settings, count=5)
    assert [row["candidate_id"] for row in draft["clips"]] == ["A", "C", "D"]
    assert diagnostics["requested_count"] == 5
    assert diagnostics["selected_count"] == 3
    assert diagnostics["shortfall"] == 2
    assert {row["topic_id"]: row["reason"] for row in diagnostics["skipped"]} == {
        "A": "duplicate_id",
        "B": "overlap",
        "E": "duration_out_of_range",
        "F": "duration_out_of_range",
    }


@pytest.mark.parametrize("count", [3, 4, 5])
def test_default_count_follows_settings_and_touching_intervals_are_allowed(count):
    analysis, previews, settings = inputs(
        [topic(str(n), 300 + n * 40, 340 + n * 40) for n in range(5)]
    )
    settings.clips = count
    draft, diagnostics = build_review_draft(analysis, previews, settings)
    assert len(draft["clips"]) == count
    assert diagnostics["shortfall"] == 0


@pytest.mark.parametrize("count", [0, 2, 6, True, 3.0])
def test_invalid_requested_count_is_rejected(count):
    analysis, previews, settings = inputs([])
    with pytest.raises(ValueError, match="3 to 5"):
        build_review_draft(analysis, previews, settings, count=count)


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


@pytest.mark.parametrize(
    "start, end, words", [(315, 355, True), (280, 320, True), (315, 355, False)]
)
def test_unsafe_boundaries_are_excluded_without_text_in_diagnostics(start, end, words):
    analysis, previews, settings = inputs(
        [topic("A", start, end), topic("B", 400, 440), topic("C", 500, 540)]
    )
    text = "合成の検証用テキスト"
    analysis["topics"][0]["text"] = text
    transcript = Transcript(
        language="ja",
        segments=[
            {
                "start": 310,
                "end": 330,
                "text": text,
                "words": [{"start": 310, "end": 330, "text": text}] if words else [],
            }
        ],
    )
    draft, diagnostics = build_review_draft(
        analysis, previews, settings, transcript=transcript
    )
    assert [row["candidate_id"] for row in draft["clips"]] == ["B", "C"]
    assert diagnostics["shortfall"] == 1
    assert diagnostics["transcript_boundary_check"] == "performed"
    assert {"topic_id": "A", "reason": "unsafe_transcript_boundary"} in diagnostics[
        "skipped"
    ]
    assert text not in json.dumps(diagnostics, ensure_ascii=False)
    assert "text" not in diagnostics


def test_safe_boundary_candidates_replace_unsafe_candidates_without_adjusting_times():
    analysis, previews, settings = inputs(
        [topic("A", 315, 355)]
        + [topic(str(n), 400 + n * 100, 440 + n * 100) for n in range(3)]
    )
    transcript = Transcript(
        language="ja",
        segments=[
            {
                "start": 310,
                "end": 330,
                "text": "合成例",
                "words": [{"start": 310, "end": 330, "text": "合成例"}],
            }
        ],
    )
    draft, diagnostics = build_review_draft(
        analysis, previews, settings, transcript=transcript
    )
    assert [(row["start"], row["end"]) for row in draft["clips"]] == [
        (400, 440),
        (500, 540),
        (600, 640),
    ]
    assert diagnostics["shortfall"] == 0


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
