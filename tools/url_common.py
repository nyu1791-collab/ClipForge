"""Small shared policy for public YouTube requests; no credentials or inference."""

import math
import re
from urllib.parse import parse_qs, urlsplit

WORKER = "nyu1791-collab/ai-model-workers"
DEEPSEEK_MODEL = "opencode/deepseek-v4.1-flash-free"
GEMINI_MODEL = "gemini-3.8-flash"
MAX_BYTES = 1024**3
MAX_SECONDS = 3600


def youtube_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Use one public YouTube video URL")
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise ValueError
        if url.hostname == "youtu.be":
            video = url.path.lstrip("/")
        elif url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if url.path == "/watch":
                ids = parse_qs(url.query).get("v", [])
                video = ids[0] if len(ids) == 1 else ""
            else:
                match = re.fullmatch(
                    r"/(?:shorts|live)/([A-Za-z0-9_-]{11})/?", url.path
                )
                video = match[1] if match else ""
        else:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            raise ValueError
    except ValueError:
        raise ValueError(
            "Use one HTTPS public YouTube video; playlists and other hosts are unsupported"
        ) from None
    return "https://www.youtube.com/watch?v=" + video


def video_profile(metadata):
    duration = metadata.get("duration")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 24 <= duration <= MAX_SECONDS
    ):
        raise ValueError(
            "First validation supports recorded videos from 24 seconds to 60 minutes"
        )
    if metadata.get("is_live") or metadata.get("live_status") in {
        "is_live",
        "is_upcoming",
        "post_live",
    }:
        raise ValueError("Wait for the public archive to finish processing")
    if metadata.get("availability") not in (None, "public", "unlisted"):
        raise ValueError(
            "Sign-in, membership, private or age-restricted videos are unsupported"
        )
    # A disclosed per-job profile, not silent removal of generated candidates.
    # Three candidate windows cover <=37.5% of the source and <=180 seconds.
    window = min(60.0, duration / 8)
    return {
        "gemini_model": GEMINI_MODEL,
        "clips": 1,
        "candidates": 3,
        "min_seconds": max(2.0, min(20.0, window / 2)),
        "max_seconds": window,
        "whisper_model": "small",
        "language": "ja",
        "max_source_seconds": MAX_SECONDS,
        "max_input_bytes": MAX_BYTES,
        "max_analysis_seconds": 180,
        "max_analysis_coverage": 0.5,
    }


def identity(request_id, source_ref):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id) or not re.fullmatch(
        r"[a-f0-9]{40}", source_ref
    ):
        raise ValueError("Invalid request identity or source revision")
