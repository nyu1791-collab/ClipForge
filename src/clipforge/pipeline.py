"""Resumable orchestration with separate proposed, rendered and failed states."""

import json
from pathlib import Path
import tempfile
import time

from . import media
from .captions import (
    FILL_BOTTOM_SHARE,
    create as create_captions,
    event_spans as caption_spans,
)
from .models import Transcript
from .storage import (
    PipelineError,
    digest,
    failure_fields,
    fingerprint,
    write_json,
)


def record_failure(output, error, current_stage):
    failure = {
        "status": "failed",
        "stage": current_stage,
        "error_type": type(error).__name__,
        "complete": False,
        "details_omitted": True,
        "human_review_performed": False,
    }
    if isinstance(error, PipelineError):
        failure["message"] = str(error)[:240]
    failure.update(failure_fields(error))
    write_json(output / "failure.json", failure)
    write_json(output / "manifest.json", {**failure, "rendered": []})


REQUIRED_FILES = {"video.mp4", "captions.ass", "captions.srt"}
OPTIONAL_FILES = {"chat.ass", "quality.json"}  # chat or a legacy quality report


def valid_receipt(target, saved, render_key):
    """True when the receipt belongs to this job and every file it lists is unchanged."""
    if not isinstance(saved, dict) or saved.get("key") != render_key:
        return False
    hashes = saved.get("sha256")
    if not isinstance(hashes, dict) or not REQUIRED_FILES <= set(hashes) <= (
        REQUIRED_FILES | OPTIONAL_FILES
    ):
        return False
    return all(
        (target / name).is_file() and digest(target / name) == expected
        for name, expected in hashes.items()
    )


# Crop sides a reviewer may request for a chat render; "auto" lets auto_layout choose.
CROP_ANCHORS = ("auto", "left", "center", "right")
# Highest caption floor a reviewer may set for one clip: the cap of auto_layout.MAX_LIFT.
MAX_CAPTION_FLOOR = 0.32


def _crop_fractions(keep):
    """Left edge of each crop side, as a fraction of the source width."""
    return {"left": 0.0, "center": (1 - keep) / 2, "right": 1 - keep}


def _auto_layout_plan(source, start, end, events, keep):
    """auto_layout.plan; imported here, since only fill chat renders need the module."""
    from . import auto_layout

    return auto_layout.plan(source, start, end, events, keep=keep)


def _auto_layout_window(source, start, end, events, anchor, keep):
    """Caption bands for a crop side the reviewer fixed: the window is not chosen by plan."""
    from . import auto_layout

    return auto_layout.plan(source, start, end, events, keep=keep, anchor=anchor)


def chat_layout(
    source, clip, transcript, settings, crop_anchor="auto", speaker_turns=None
):
    """Crop position and caption lifts for a chat render with fit fill.

    crop_anchor "auto" takes auto_layout's choice; left, center and right fix the crop and only
    place the captions. When the plan fails the picture is centred (or on the fixed side) and
    every caption stays at the base margin (lifts None); reason then names the failure. With
    caption_lift off the crop is kept and the lifts are dropped (reason "disabled"). Caption
    times are the file's own, as auto_layout expects.
    """
    frame_width, _ = media.frame_size(settings)
    keep = (frame_width - settings.chat_panel_pixels()) / frame_width
    try:
        events = [
            (clip.start + start, clip.start + end)
            for start, end in caption_spans(
                transcript,
                clip,
                settings,
                **({"speaker_turns": speaker_turns} if speaker_turns else {}),
            )
        ]
        if crop_anchor == "auto":
            plan = _auto_layout_plan(source, clip.start, clip.end, events, keep)
        else:
            plan = _auto_layout_window(
                source, clip.start, clip.end, events, crop_anchor, keep
            )
        lifts = [float(lift) for lift in plan["lifts"]]
        if len(lifts) != len(events):
            raise PipelineError("auto layout gave one lift per caption")
        crop = plan["crop"]
        # The crop stays inside the source: its left edge is 0 to the share not kept.
        x = min(max(float(crop["x"]), 0.0), 1 - keep)
        enabled = settings.caption_lift
        return {
            "anchor": crop["anchor"],
            "requested": crop_anchor,
            "x": x,
            "lifts": lifts if enabled else None,
            "static": plan.get("bands") if enabled else None,
            "reason": None if enabled else "disabled",
        }
    except PipelineError as failure:
        fixed = crop_anchor != "auto"
        return {
            "anchor": crop_anchor if fixed else "center",
            "requested": crop_anchor,
            "x": _crop_fractions(keep)[crop_anchor] if fixed else None,
            "lifts": None,
            "static": None,
            "reason": str(failure)[:200],
        }


def standard_layout(source, clip, transcript, settings, speaker_turns=None):
    """Caption lifts for a 16:9 render without the chat column, from the source picture.

    The window is the whole picture, or the centred crop of a source wider than 16:9 when framing
    is center-crop. A failed analysis keeps the base margin.
    """
    base = {
        "anchor": "full",
        "requested": "auto",
        "x": None,
        "lifts": None,
        "static": None,
        "reason": None,
    }
    if not settings.caption_lift:
        return {**base, "reason": "disabled"}
    try:
        info = media.probe(source)
        frame_width, frame_height = media.frame_size(settings)
        # Share of the source width the picture keeps: all of it unless center-crop cuts it.
        share = 1.0
        if settings.framing == "center-crop":
            share = min(
                1.0, frame_width * info["height"] / (frame_height * info["width"])
            )
        events = [
            (clip.start + start, clip.start + end)
            for start, end in caption_spans(
                transcript,
                clip,
                settings,
                **({"speaker_turns": speaker_turns} if speaker_turns else {}),
            )
        ]
        plan = _auto_layout_window(
            source, clip.start, clip.end, events, "center", share
        )
        lifts = [float(lift) for lift in plan["lifts"]]
        if len(lifts) != len(events):
            raise PipelineError("auto layout gave one lift per caption")
        return {
            **base,
            "anchor": "center" if share < 1 else "full",
            "x": plan["crop"]["x"],
            "lifts": lifts,
            "static": plan["bands"],
            "reason": None,
        }
    except PipelineError as failure:
        return {**base, "reason": str(failure)[:200]}


def _with_floor(layout, count, floor):
    """Raises each caption's lift to the reviewed floor; without lifts the floor starts from base."""
    from . import auto_layout

    lifts = (
        layout["lifts"] if layout["lifts"] is not None else [FILL_BOTTOM_SHARE] * count
    )
    return {**layout, "lifts": auto_layout.apply_floor(lifts, floor), "floor": floor}


def landscape_layout(
    source, clip, transcript, settings, crop_anchor, caption_floor, speaker_turns=None
):
    """The caption layout of a 16:9 render, or None when the render has none.

    With the chat panel's fill fit the layout is chat_layout's (crop and lifts); the other chat fits
    have none. Without the chat panel the layout is standard_layout, unless caption_lift is off and
    no caption_floor was reviewed. Portrait renders have no layout.
    """
    if settings.aspect != "16:9":
        if caption_floor is not None:
            raise PipelineError("caption_floor needs a landscape 16:9 render")
        return None
    if settings.chat_panel:
        if settings.chat_panel_fit != "fill":
            # The other chat fits keep their captions in the band or on a fixed margin.
            if caption_floor is not None:
                raise PipelineError(
                    "caption_floor needs the chat fill fit or no chat panel"
                )
            return None
        layout = chat_layout(
            source,
            clip,
            transcript,
            settings,
            crop_anchor,
            **({"speaker_turns": speaker_turns} if speaker_turns else {}),
        )
    elif settings.caption_lift or caption_floor is not None:
        layout = standard_layout(
            source,
            clip,
            transcript,
            settings,
            **({"speaker_turns": speaker_turns} if speaker_turns else {}),
        )
    else:
        return None
    if caption_floor is None:
        return layout
    count = len(
        caption_spans(
            transcript,
            clip,
            settings,
            **({"speaker_turns": speaker_turns} if speaker_turns else {}),
        )
    )
    return _with_floor(layout, count, caption_floor)


def _chat_inputs(chat):
    """What a chat-panel render depends on: the fingerprint of its ASS file and the chat reason."""
    return {"ass_sha256": fingerprint(chat["ass"]), "reason": chat["reason"]}


def _layout_inputs(layout):
    """What a caption-layout render depends on; None when the render has no layout."""
    if layout is None:
        return None
    return {
        "x": layout["x"],
        "lifts": layout["lifts"],
        "requested": layout["requested"],
        **({"floor": layout["floor"]} if layout.get("floor") is not None else {}),
    }


def _render_key(key, clip, transcript, chat_inputs, layout_inputs):
    """The key of one clip render. Chat and layout join it only when the render has them, so a
    render without either keeps the key it had before they were added.
    """
    parts = {
        "job": key,
        "clip": clip.model_dump(),
        "transcript": transcript.model_dump(),
    }
    if chat_inputs is not None:
        # The field keeps the name "ass" that earlier receipts were keyed by.
        parts["chat"] = {
            "ass": chat_inputs["ass_sha256"],
            "reason": chat_inputs["reason"],
        }
    if layout_inputs is not None:
        parts["layout"] = layout_inputs
    return fingerprint(parts)


def _stored_inputs(saved):
    """The render inputs a receipt was keyed by; None for a receipt written without them."""
    inputs = saved.get("render_inputs") if isinstance(saved, dict) else None
    if isinstance(inputs, dict) and {"chat", "layout"} <= inputs.keys():
        return inputs
    return None


def saved_render_inputs(root, index):
    """The render inputs of clip index under root, or None when it has no receipt with them."""
    receipt = root / f"clip-{index:03}" / "render.json"
    if not receipt.is_file():
        return None
    return _stored_inputs(json.loads(receipt.read_text()))


def _has_layout(settings, caption_floor):
    """Whether a render with these settings gets a caption layout; landscape_layout's rule, without
    the analysis.
    """
    if settings.aspect != "16:9":
        return False
    if settings.chat_panel:
        return settings.chat_panel_fit == "fill"
    return settings.caption_lift or caption_floor is not None


def _same_layout_arguments(saved_layout, settings, crop_anchor, caption_floor):
    """Whether this call lays a clip out as the stored render was laid out.

    saved_layout is the stored layout inputs, or None for a render without a layout. A call that
    would lay the clip out differently is refused rather than answered with the stored clip.
    """
    if saved_layout is None:
        # The chat crop side only matters with the chat fill, which has a layout.
        return not (
            _has_layout(settings, caption_floor)
            or (settings.chat_panel and crop_anchor != "auto")
        )
    # Standard layouts always record "auto" as the requested side.
    requested = crop_anchor if settings.chat_panel else "auto"
    return (
        _has_layout(settings, caption_floor)
        and saved_layout.get("requested") == requested
        and saved_layout.get("floor") == caption_floor
    )


def _existing_clip(target, name, saved, render_key):
    """The stored result of a rendered clip; refused when its receipt or files no longer match."""
    if not valid_receipt(target, saved, render_key):
        raise PipelineError("existing clip differs from the saved render receipt")
    return {"directory": "clips/" + name, **saved}


def render_clip(
    source,
    root,
    index,
    clip,
    transcript,
    settings,
    key,
    chat=None,
    crop_anchor="auto",
    caption_floor=None,
    speaker_turns=None,
):
    """Render one clip. chat is {"ass", "messages", "reason", "error"} with the chat panel.

    caption_floor is a reviewed share of the height (0 to MAX_CAPTION_FLOOR) that every caption's
    lift is raised to; it applies to 16:9 renders only. A clip rendered before with its inputs
    saved in render.json is returned as stored: its chat and layout are not fetched or planned
    again, so chat may be None for it.
    """
    name = f"clip-{index:03}"
    if speaker_turns:
        from .speaker_captions import validate_turns

        speaker_turns = validate_turns(speaker_turns, clip.end - clip.start)
        # Keep old receipt keys when no annotations exist. Changing a speaker annotation
        # must never reuse video burned with another color or another caption boundary.
        key = fingerprint({"job": key, "speaker_turns": speaker_turns})
    target = root / name
    if crop_anchor not in CROP_ANCHORS:
        raise PipelineError("crop_anchor must be auto, left, center or right")
    if caption_floor is not None and (
        type(caption_floor) not in (int, float)
        or not 0 <= caption_floor <= MAX_CAPTION_FLOOR
    ):
        raise PipelineError("caption_floor must be from 0 to 0.32")
    existing = None
    if target.exists():
        receipt = target / "render.json"
        if not receipt.is_file():
            raise PipelineError("existing clip has no verified render receipt")
        existing = json.loads(receipt.read_text())
        inputs = _stored_inputs(existing)
        if inputs is not None:
            if not _same_layout_arguments(
                inputs["layout"], settings, crop_anchor, caption_floor
            ):
                raise PipelineError(
                    "existing clip differs from the saved render receipt"
                )
            return _existing_clip(
                target,
                name,
                existing,
                _render_key(
                    key,
                    clip,
                    transcript,
                    inputs["chat"],
                    inputs["layout"],
                ),
            )
        # A receipt without saved inputs is reused when its key has neither chat nor layout.
        if valid_receipt(
            target, existing, _render_key(key, clip, transcript, None, None)
        ):
            if not _same_layout_arguments(None, settings, crop_anchor, caption_floor):
                raise PipelineError(
                    "existing clip differs from the saved render receipt"
                )
            return {"directory": "clips/" + name, **existing}
    if settings.chat_panel and chat is None:
        raise PipelineError("chat panel needs a Kick VOD URL source")
    timings = {}
    # The layout (chat crop and lifts, or caption lifts) is planned before the render key, so a
    # different plan is a different render.
    layout = landscape_layout(
        source,
        clip,
        transcript,
        settings,
        crop_anchor,
        caption_floor,
        **({"speaker_turns": speaker_turns} if speaker_turns else {}),
    )
    chat_inputs = _chat_inputs(chat) if settings.chat_panel else None
    layout_inputs = _layout_inputs(layout)
    render_key = _render_key(key, clip, transcript, chat_inputs, layout_inputs)
    if existing is not None:
        return _existing_clip(target, name, existing, render_key)
    with tempfile.TemporaryDirectory(prefix="." + name + "-", dir=root) as temporary:
        directory = Path(temporary)
        if settings.chat_panel:
            # Written beside the MP4 under the fixed name the render graph uses.
            (directory / "chat.ass").write_text(chat["ass"], encoding="utf-8")
        caption = timed(
            timings,
            "captions",
            lambda: create_captions(
                transcript,
                clip,
                directory,
                settings,
                lifts=layout["lifts"] if layout else None,
                **({"speaker_turns": speaker_turns} if speaker_turns else {}),
            ),
        )
        info = timed(
            timings,
            "encode",
            lambda: media.render(
                source,
                directory / "video.mp4",
                clip,
                settings,
                ass=directory / "captions.ass",
                **({"chat_ass": directory / "chat.ass"} if settings.chat_panel else {}),
                # Only the chat fill crops by position; standard layouts keep the framing setting.
                **({"crop_x": layout["x"]} if layout and settings.chat_panel else {}),
            ),
        )
        report = {"status": "not_run", "passed": None, "verification_scope": "none"}
        names = sorted(
            REQUIRED_FILES | {n for n in OPTIONAL_FILES if (directory / n).is_file()}
        )
        saved = {
            "key": render_key,
            "selection": clip.model_dump(),
            "status": "rendered",
            "media": info,
            "captions": caption,
            "quality": report,
            "sha256": {n: digest(directory / n) for n in names},
            # The inputs the key was built from; a rerun rebuilds the key from them.
            "render_inputs": {"chat": chat_inputs, "layout": layout_inputs},
            # Seconds per stage; not part of the render key or the hashed files.
            "timings": timings,
        }
        if settings.chat_panel:
            # Diagnostics: an unavailable chat leaves the column empty and records why.
            saved["chat"] = {
                "messages": chat["messages"],
                "reason": chat["reason"],
                "error": chat["error"],
            }
        if layout:
            lifts = layout["lifts"] or []
            saved["layout"] = {
                "anchor": layout["anchor"],
                "requested": layout["requested"],
                "x": layout["x"],
                "lift_min": min(lifts, default=None),
                "lift_max": max(lifts, default=None),
                "lifted": sum(lift > FILL_BOTTOM_SHARE + 1e-9 for lift in lifts),
                "reason": layout["reason"],
                "floor": layout.get("floor"),
                "static": layout.get("static"),
            }
        write_json(directory / "render.json", saved)
        # Commit the whole clip directory only when MP4 and captions are complete.
        if target.exists():
            raise PipelineError("clip output already exists")
        directory.rename(target)
    return {"directory": "clips/" + name, **saved}


def timed(timings, name, function, *args):
    """Run one stage and record its wall-clock seconds in the job's timings."""
    started = time.monotonic()
    try:
        return function(*args)
    finally:
        timings[name] = round(time.monotonic() - started, 2)


def saved_transcript(settings):
    """The verified analysis's transcript named by settings.transcript_key, or None.

    Recognition happens only in the ASR job on Colab; every later stage reads its result.
    """
    if not settings.transcript_cache or not settings.transcript_key:
        return None
    path = Path(settings.transcript_cache) / (settings.transcript_key + ".json")
    if path.is_file():
        try:
            return Transcript.model_validate_json(
                path.read_text(encoding="utf-8")
            ).model_dump()
        except (ValueError, UnicodeDecodeError):
            pass
    return None


def load_transcript(settings, receipt):
    """The saved transcript, recorded in receipt; recognition is never started here."""
    value = saved_transcript(settings)
    if value is None:
        raise PipelineError(
            "a matching valid transcript cache is required; ASR was not started"
        )
    receipt.update(
        enabled=True,
        key=settings.transcript_key,
        reused=True,
        source="transcript_cache",
    )
    return value
