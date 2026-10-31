"""Long-form selections from saved transcript topics without an approval gate."""

from .storage import PipelineError
from .topic_hints import VOLUME_WEIGHT, CHAT_WEIGHT, hint_topics
from .storage import TIME_TOLERANCE


def build_review_draft(
    analysis,
    previews,
    settings,
    *,
    transcript=None,
    count=None,
    volume=None,
    volume_reason=None,
    chat=None,
    chat_diagnostics=None,
):
    """Return review-file-shaped suggestions and text-free selection diagnostics.

    Preview rows with a file and receipt represent successful acquisitions. Failed
    rows, if supplied, must not be treated as available review media. ``volume`` is
    the optional one-second loudness series; without it the ranking is unchanged and
    ``volume_reason`` records why it is missing.
    """
    count = settings.clips if count is None else count
    if previews.get("analysis_key") != analysis["analysis_key"]:
        raise PipelineError("previews belong to a different audio analysis")
    available = {
        row["topic_id"]
        for row in previews["previews"]
        if row.get("file")
        and isinstance(row.get("receipt"), dict)
        and row.get("status") in (None, "success")
        and not row.get("error")
    }
    if previews.get("mode") == "transcript":
        available = {row["id"] for row in analysis["topics"]}
    clips, skipped = [], []
    selected_ids = set()
    ranked = hint_topics(analysis, volume=volume, chat=chat)["topics"]
    for topic in ranked:
        topic_id, start, end = topic["id"], topic["start"], topic["end"]
        reason = None
        if topic_id not in available:
            reason = "preview_unavailable"
        elif topic_id in selected_ids:
            reason = "duplicate_id"
        elif not 0 <= start < end <= analysis["duration"] + TIME_TOLERANCE:
            reason = "invalid_interval"
        elif any(start < clip["end"] and clip["start"] < end for clip in clips):
            reason = "overlap"
        if reason is not None:
            skipped.append({"topic_id": topic_id, "reason": reason})
            continue
        clips.append(
            {
                "candidate_id": topic_id,
                "start": start,
                "end": end,
                "title": "配信の話題：" + topic_id,
                "reason": "保存済みASRの話題順位から選択。",
                "confidence": 0.0,
                "video_reviewed": False,
            }
        )
        selected_ids.add(topic_id)
        if len(clips) == count:
            break
    draft = {"analysis_key": analysis["analysis_key"], "clips": clips}
    diagnostics = {
        "analysis_key": analysis["analysis_key"],
        "status": "selection_ready",
        "ranking_method": "topic_hints",
        "semantic_topic_review": "not_required",
        "human_review_performed": False,
        "transcript_boundary_check": "not_run",
        "duration_target_seconds": [settings.min_seconds, settings.max_seconds],
        "outside_duration_target": [
            clip["candidate_id"]
            for clip in clips
            if not (
                settings.min_seconds
                <= clip["end"] - clip["start"]
                <= settings.max_seconds
            )
        ],
        "requested_count": count,
        "selected_count": len(clips),
        "shortfall": count - len(clips),
        "skipped": skipped,
        "volume_hints": _volume_diagnostics(ranked, volume, volume_reason),
    }
    if chat is not None or chat_diagnostics is not None:
        diagnostics["chat_hints"] = {
            **(chat_diagnostics or {"data": "available"}),
            "weight": CHAT_WEIGHT if chat is not None else 0.0,
            "topics": [
                {
                    "topic_id": hint["id"],
                    "score": hint.get("chat_score"),
                    "messages": hint.get("chat_messages"),
                    "observed_seconds": hint.get("chat_coverage_seconds", 0),
                }
                for hint in ranked
            ],
        }
    return draft, diagnostics


def _volume_diagnostics(ranked, volume, reason):
    if volume is None:
        return {
            "data": "not_available",
            "reason": reason or "not_provided",
            "weight": 0.0,
            "topics": [],
        }
    return {
        "data": "available",
        "weight": VOLUME_WEIGHT,
        "topics": [
            {
                "topic_id": hint["id"],
                "bump_db": hint["volume_bump_db"],
                "score": hint["volume_score"],
            }
            for hint in ranked
        ],
    }
