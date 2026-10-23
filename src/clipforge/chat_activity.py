"""Bounded, text-free Kick chat activity for the already shortlisted preview intervals."""

import math
from urllib.parse import urlsplit

from .storage import PipelineError


def candidate_series(vod, analysis, previews):
    """Return observed one-second counts and diagnostics; never fetch a full VOD."""
    url = getattr(vod, "url", None)
    if not isinstance(url, str) or urlsplit(url).hostname != "kick.com":
        return None, {
            "data": "not_available",
            "reason": "unsupported_source",
            "intervals": 0,
        }
    from . import kick_chat, kick_media

    try:
        kick_media.canonical(url)
    except PipelineError:
        return None, {
            "data": "not_available",
            "reason": "unsupported_source",
            "intervals": 0,
        }
    topics = {row["id"]: row for row in analysis["topics"]}
    intervals = []
    for row in previews["previews"]:
        topic = topics.get(row["topic_id"])
        if (
            topic is not None
            and row.get("file")
            and not row.get("error")
            and row.get("status") in (None, "success")
        ):
            intervals.append((topic["start"], topic["end"]))
    merged = []
    for start, end in sorted(intervals):
        if not 0 <= start < end <= analysis["duration"]:
            raise ValueError("invalid shortlisted chat interval")
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    channel, _, uuid = urlsplit(url).path.strip("/").split("/")
    counts, failures = {}, 0
    for start, end in merged:
        try:
            messages = kick_chat.fetch_messages(channel, uuid, start, end, lead=0)
        except (PipelineError, OSError, ValueError):
            failures += 1
            continue
        observed = {i: 0 for i in range(math.floor(start), math.ceil(end))}
        for message in messages:
            moment = float(message["t"])
            if start <= moment < end:
                observed[math.floor(moment)] += 1
        for moment, count in observed.items():
            counts[moment] = counts.get(moment, 0) + count
    info = {
        "data": "available"
        if counts and not failures
        else "partial"
        if counts
        else "not_available",
        "reason": "fetch_failed" if failures else None,
        "intervals": len(merged),
        "failed_intervals": failures,
        "observed_seconds": len(counts),
    }
    return sorted(counts.items()) if counts else None, info
