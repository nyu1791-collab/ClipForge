"""Unapproved review drafts from previewed topics; hints do not judge meaning."""

from .audio_first import shifted_transcript
from .storage import PipelineError
from .topic_hints import VOLUME_WEIGHT, hint_topics
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
):
    """Return review-file-shaped suggestions and text-free selection diagnostics.

    Preview rows with a file and receipt represent successful acquisitions. Failed
    rows, if supplied, must not be treated as available review media. ``volume`` is
    the optional one-second loudness series; without it the ranking is unchanged and
    ``volume_reason`` records why it is missing.
    """
    count = settings.clips if count is None else count
    if type(count) is not int or not 3 <= count <= 5:
        raise ValueError("review drafts require 3 to 5 requested clips")
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
    clips, skipped = [], []
    selected_ids = set()
    ranked = hint_topics(analysis, volume=volume)["topics"]
    for topic in ranked:
        topic_id, start, end = topic["id"], topic["start"], topic["end"]
        reason = None
        if topic_id not in available:
            reason = "preview_unavailable"
        elif topic_id in selected_ids:
            reason = "duplicate_id"
        elif not 0 <= start < end <= analysis["duration"] + TIME_TOLERANCE:
            reason = "invalid_interval"
        elif not settings.min_seconds <= end - start <= settings.max_seconds:
            reason = "duration_out_of_range"
        elif any(start < clip["end"] and clip["start"] < end for clip in clips):
            reason = "overlap"
        elif transcript is not None:
            try:
                shifted_transcript(transcript, start, end)
            except PipelineError:
                reason = "unsafe_transcript_boundary"
        if reason is not None:
            skipped.append({"topic_id": topic_id, "reason": reason})
            continue
        clips.append(
            {
                "candidate_id": topic_id,
                "start": start,
                "end": end,
                "title": "要確認：" + topic_id,
                "reason": "ヒント順位に基づく下書き。意味・映像は未確認。",
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
        "status": "awaiting_video_review",
        "ranking_method": "topic_hints",
        "semantic_topic_review": "required",
        "human_review_performed": False,
        "transcript_boundary_check": "performed"
        if transcript is not None
        else "not_available",
        "requested_count": count,
        "selected_count": len(clips),
        "shortfall": count - len(clips),
        "skipped": skipped,
        "volume_hints": _volume_diagnostics(ranked, volume, volume_reason),
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
