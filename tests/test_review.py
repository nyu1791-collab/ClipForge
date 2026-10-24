import copy
import importlib.util
from pathlib import Path

import pytest

from clipforge import audio_first, review
from clipforge.storage import PipelineError


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("crop_anchor", "top", "crop_anchor"),
        ("caption_floor", True, "caption_floor"),
        ("caption_floor", "0.2", "caption_floor"),
        ("caption_floor", -0.01, "caption_floor"),
        ("caption_floor", 0.33, "caption_floor"),
        ("caption_floor", float("nan"), "caption_floor"),
        ("caption_floor", float("inf"), "caption_floor"),
    ],
)
def test_gateway_and_worker_reject_the_same_review_before_work(field, value, message):
    spec = importlib.util.spec_from_file_location(
        "review_stage_tool", Path("tools/submit_audio_stage.py")
    )
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    payload = {"clips": [{"video_reviewed": True, field: value}]}
    with pytest.raises(gateway.AudioStageError, match=message):
        gateway.require_video_review(payload)
    # No analysis, settings or media access is needed to reject the invalid approval.
    with pytest.raises(PipelineError, match=message):
        audio_first.reviewed_clips(payload, None, None, None)


@pytest.mark.parametrize(
    "value",
    [None, {}, {"clips": []}, {"clips": [None]}, {"clips": [{"video_reviewed": 1}]}],
)
def test_empty_or_unapproved_reviews_are_rejected(value):
    with pytest.raises(PipelineError, match="requires video review"):
        review.require_video_review(value)


def test_approval_validation_preserves_reviewed_overrides():
    value = {
        "clips": [
            {"video_reviewed": True, "crop_anchor": "right", "caption_floor": 0.32}
        ]
    }
    original = copy.deepcopy(value)
    review.require_video_review(value)
    assert audio_first.crop_anchors(value) == ["right"]
    assert audio_first.caption_floors(value) == [0.32]
    assert value == original
