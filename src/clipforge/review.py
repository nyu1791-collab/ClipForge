"""Lightweight approval and layout contracts shared by gateways and render workers."""

import math

from .storage import PipelineError

CROP_ANCHORS = ("auto", "left", "center", "right")
MAX_CAPTION_FLOOR = 0.32


def crop_anchor(value, error_type=PipelineError):
    if value not in CROP_ANCHORS:
        raise error_type("crop_anchor must be auto, left, center or right")
    return value


def caption_floor(value, error_type=PipelineError):
    if value is not None and (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= MAX_CAPTION_FLOOR
    ):
        raise error_type("caption_floor must be from 0 to 0.32")
    return value


def require_video_review(value, error_type=PipelineError):
    """Check attestation and layout values; source/topic identity is checked by the worker."""
    clips = value.get("clips") if isinstance(value, dict) else None
    if (
        not isinstance(clips, list)
        or not clips
        or any(
            not isinstance(row, dict) or row.get("video_reviewed") is not True
            for row in clips
        )
    ):
        raise error_type("each selected topic requires video review")
    for row in clips:
        crop_anchor(row.get("crop_anchor", "auto"), error_type)
        caption_floor(row.get("caption_floor"), error_type)
