"""Anonymous, audio-grounded speaker annotations; never guess from transcript text."""

from collections import Counter
import math

from .storage import PipelineError

MIN_CONFIDENCE = 0.8
SPEAKER_COLORS = {
    "speaker_1": (255, 255, 255),
    "speaker_2": (110, 220, 255),
    "speaker_3": (255, 200, 115),
    "speaker_4": (170, 245, 170),
    "speaker_5": (235, 180, 255),
    "speaker_6": (255, 175, 200),
}


def validate_turns(rows, duration):
    """Clamp display spans; unusable annotations never block subtitle rendering."""
    if not isinstance(rows, list) or len(rows) > 10000:
        raise PipelineError("speaker_turns must be a bounded list")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start, end = row.get("start"), row.get("end")
        if any(
            type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)
        ):
            continue
        start, end = max(0, start), min(duration, end)
        if start >= end:
            continue
        speaker, confidence = row.get("speaker_id"), row.get("confidence", 0)
        certain = (
            isinstance(speaker, str)
            and speaker in SPEAKER_COLORS
            and type(confidence) in (int, float)
            and math.isfinite(confidence)
            and MIN_CONFIDENCE <= confidence <= 1
        )
        normalized.append(
            {
                "start": start,
                "end": end,
                "speaker_id": speaker if certain else None,
                "confidence": confidence if certain else 0,
            }
        )
    return sorted(normalized, key=lambda r: (r["start"], r["end"]))


def speaker_for_token(start, end, turns):
    """Color only tokens wholly covered by one confident identity, without ambiguity."""
    covered = []
    for row in turns:
        if row["end"] <= start + 0.000001 or row["start"] >= end - 0.000001:
            continue
        if (
            row["start"] > start + 0.001
            or row["end"] < end - 0.001
            or row["confidence"] < MIN_CONFIDENCE
            or row["speaker_id"] is None
        ):
            return None
        covered.append(row["speaker_id"])
    return covered[0] if covered and len(set(covered)) == 1 else None


def assignments_to_turns(value, slots, clip_count, uncertain=()):
    """Map Gemini segment IDs to saved ASR spans, never to model-generated times."""
    result = [[] for _ in range(clip_count)]
    diagnostics = {"accepted": 0, "neutral": 0, "unknown_ids": [], "duplicate_ids": []}
    rows = value.get("speaker_assignments", [])
    if value.get("audio_access") != "heard" or not isinstance(rows, list):
        return result, diagnostics
    counts = Counter(
        r.get("segment_id")
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("segment_id"), str)
    )
    by_id = {
        r["segment_id"]: r
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("segment_id"), str)
    }
    diagnostics["unknown_ids"] = sorted(set(by_id) - set(slots))
    diagnostics["duplicate_ids"] = sorted(sid for sid, n in counts.items() if n > 1)
    for sid, slot in slots.items():
        row = by_id.get(sid, {})
        speaker, confidence = row.get("speaker_id"), row.get("confidence", 0)
        certain = (
            counts[sid] == 1
            and sid not in uncertain
            and isinstance(speaker, str)
            and speaker in SPEAKER_COLORS
            and type(confidence) in (int, float)
            and math.isfinite(confidence)
            and MIN_CONFIDENCE <= confidence <= 1
        )
        turn = {
            "start": slot["start"],
            "end": slot["end"],
            "speaker_id": speaker if certain else None,
            "confidence": confidence if certain else 0,
        }
        turns = result[slot["clip"] - 1]
        # Only consecutive, confidently matching segments may share a span. An unknown
        # segment is retained as a neutral boundary, even between the same voice.
        if (
            certain
            and turns
            and turns[-1]["speaker_id"] == speaker
            and 0 <= turn["start"] - turns[-1]["end"] <= 0.7
        ):
            turns[-1]["end"] = turn["end"]
            turns[-1]["confidence"] = min(turns[-1]["confidence"], confidence)
        else:
            turns.append(turn)
        diagnostics["accepted" if certain else "neutral"] += 1
    return result, diagnostics
