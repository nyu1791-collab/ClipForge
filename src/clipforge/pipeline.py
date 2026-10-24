"""Resumable orchestration with separate proposed, rendered and failed states."""

import json
from pathlib import Path
import shutil
import tempfile
import time


from . import (
    assets,
    diagrams,
    fonts,
    media,
    quality,
    signals,
    silence,
    tables,
    thumbnail,
)
from .captions import (
    FILL_BOTTOM_SHARE,
    create as create_captions,
    event_spans as caption_spans,
)
from .hardware import asr_options, cpu_threads
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


REQUIRED_FILES = {
    "video.mp4",
    "captions.ass",
    "captions.srt",
    "ledger.json",
    "quality.json",
}
OPTIONAL_FILES = {"thumbnail.png", "chat.ass"}  # chat.ass only with the chat panel
VISUAL_DIR = "visuals"
SUPPLIED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
REPOSITORY_URL = "https://github.com/nyu1791-collab/ClipForge"


def visual_names(ledger):
    """Overlay files a clip receipt lists, relative to the clip directory."""
    return {f"{VISUAL_DIR}/{entry['file']}" for entry in ledger}


def valid_receipt(target, saved, render_key):
    """True when the receipt belongs to this job and every file it lists is unchanged."""
    if not isinstance(saved, dict) or saved.get("key") != render_key:
        return False
    hashes, ledger = saved.get("sha256"), saved.get("ledger")
    if not isinstance(hashes, dict) or not isinstance(ledger, list):
        return False
    for entry in ledger:
        name = entry.get("file") if isinstance(entry, dict) else None
        if not isinstance(name, str) or Path(name).name != name:
            return False
    if (
        not REQUIRED_FILES
        <= set(hashes)
        <= REQUIRED_FILES | OPTIONAL_FILES | visual_names(ledger)
    ):
        return False
    files_intact = all(
        (target / name).is_file() and digest(target / name) == expected
        for name, expected in hashes.items()
    )
    # render.json is not hashed itself, so its ledger must match the hashed ledger.json.
    return files_intact and json.loads((target / "ledger.json").read_text()) == ledger


def prepare_visuals(clip, settings, directory, duration):
    """Overlays from supplied files, original diagrams and licensed downloads.

    Each overlay is copied into the clip's visuals/ folder and gets a ledger entry
    (file, hash, source, credit, licence), so the clip keeps what it used. A supplied
    file without a complete source is refused.
    """
    folder = directory / VISUAL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    visuals, ledger = [], []
    for index, visual in enumerate(settings.visuals, 1):
        if not (visual.source_credit and visual.source_url and visual.license):
            raise PipelineError("visual needs source_credit, source_url and license")
        source = Path(visual.file)
        if not source.is_file():
            raise PipelineError("visual file is missing")
        suffix = source.suffix.lower()
        if suffix == ".svg":
            target = folder / f"visual-{index:02}.png"
            diagrams.svg_to_png(source, target)
        elif suffix in SUPPLIED_SUFFIXES:
            target = folder / f"visual-{index:02}{suffix}"
            shutil.copy2(source, target)
            assets.check_image(target)
        else:
            raise PipelineError("visual must be an SVG, PNG, JPEG or WebP file")
        ledger.append(
            assets.validate_entry(
                {
                    "id": f"visual-{index:02}",
                    "file": target.name,
                    "kind": "supplied",
                    "sha256": digest(target),
                    "source_url": visual.source_url,
                    "source_credit": visual.source_credit,
                    "license": visual.license,
                }
            )
        )
        visuals.append(
            {
                "file": str(target),
                "start": visual.start,
                "end": visual.end,
                "position": visual.position,
                "max_fraction": visual.max_fraction,
            }
        )
    if settings.diagrams:
        font_path = fonts.resolve(settings.font)
        for index, diagram in enumerate(settings.diagrams, 1):
            png = folder / f"diagram-{index:02}.png"
            diagrams.render(diagram, font_path, png)
            ledger.append(
                assets.validate_entry(
                    {
                        "id": f"diagram-{index:02}",
                        "file": png.name,
                        "kind": "diagram",
                        "sha256": digest(png),
                        "source_url": REPOSITORY_URL,
                        "source_credit": "作成: ClipForge（独自図解）",
                        "license": "ClipForge original",
                    }
                )
            )
            visuals.append(
                {
                    "file": str(png),
                    "start": diagram.start,
                    "end": diagram.end,
                    "position": "center",
                    "max_fraction": 0.85,
                }
            )
    if settings.image_search and clip.title:
        items = assets.search(clip.title, settings.image_sources, 2)
        downloaded = assets.download_all(items, folder)
        slot = duration / max(1, len(downloaded))
        for number, entry in enumerate(downloaded):
            ledger.append(assets.validate_entry(entry))
            visuals.append(
                {
                    "file": str(folder / entry["file"]),
                    "start": number * slot,
                    "end": (number + 1) * slot,
                    "position": "right",
                    "max_fraction": 0.38,
                }
            )
    if settings.table_query:
        found = tables.search(settings.table_query)
        if found:
            # One original rendering of the best table, with its article as the source.
            table = found[0]
            png = folder / "table-01.png"
            diagrams.table(
                table["title"][:60], table["rows"], fonts.resolve(settings.font), png
            )
            ledger.append(
                assets.validate_entry(
                    {
                        "id": "table-01",
                        "file": png.name,
                        "kind": "table",
                        "sha256": digest(png),
                        "source_url": table["source_url"],
                        "source_credit": table["source_credit"],
                        "license": table["license"],
                    }
                )
            )
            start = duration / 2
            visuals.append(
                {
                    "file": str(png),
                    "start": start,
                    "end": start + min(8.0, duration / 3),
                    "position": "center",
                    "max_fraction": 0.85,
                }
            )
    return visuals, ledger


def cut_regions(source, transcript, clip, settings):
    """Original-time intervals to keep when silence cutting is enabled, else None."""
    if not settings.silence_cut:
        return None
    if settings.vad == "transcript":
        spans = silence.transcript_spans(transcript, clip.start, clip.end)
    else:
        spans = silence.silero_spans(source, clip.start, clip.end)
    regions = silence.keep_regions(
        spans, clip.start, clip.end, settings.silence_pad, settings.silence_min_gap
    )
    return regions or None


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


def chat_layout(source, clip, transcript, settings, crop_anchor="auto"):
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
            for start, end in caption_spans(transcript, clip, settings)
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


def _source_time(moment, clip, keep):
    """Source-file seconds of a caption time on the output timeline (after silence cuts, if any)."""
    if not keep:
        return clip.start + moment
    elapsed = 0.0
    for start, end in keep:
        if moment <= elapsed + (end - start):
            return start + max(0.0, moment - elapsed)
        elapsed += end - start
    return keep[-1][1]


def standard_layout(source, clip, transcript, settings, keep):
    """Caption lifts for a 16:9 render without the chat column, from the source picture.

    The window is the whole picture, or the centred crop of a source wider than 16:9 when framing
    is center-crop. The panels layout stacks two boxes, so its picture is not the source's: it
    keeps the base margin (reason "panels"). A failed analysis also keeps the base margin.
    """
    base = {
        "anchor": "full",
        "requested": "auto",
        "x": None,
        "lifts": None,
        "static": None,
        "reason": None,
    }
    if settings.layout == "panels":
        return {**base, "reason": "panels"}
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
            (_source_time(start, clip, keep), _source_time(end, clip, keep))
            for start, end in caption_spans(transcript, clip, settings, keep)
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
    source, clip, transcript, settings, keep, crop_anchor, caption_floor
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
        layout = chat_layout(source, clip, transcript, settings, crop_anchor)
    elif settings.caption_lift or caption_floor is not None:
        layout = standard_layout(source, clip, transcript, settings, keep)
    else:
        return None
    if caption_floor is None:
        return layout
    count = len(caption_spans(transcript, clip, settings, keep))
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
):
    """Render one clip. chat is {"ass", "messages", "reason", "error"} with the chat panel.

    caption_floor is a reviewed share of the height (0 to MAX_CAPTION_FLOOR) that every caption's
    lift is raised to; it applies to 16:9 renders only. A clip rendered before with its inputs
    saved in render.json is returned as stored: its chat and layout are not fetched or planned
    again, so chat may be None for it.
    """
    name = f"clip-{index:03}"
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
    keep = timed(timings, "silence", cut_regions, source, transcript, clip, settings)
    # The layout (chat crop and lifts, or caption lifts) is planned before the render key, so a
    # different plan is a different render.
    layout = landscape_layout(
        source, clip, transcript, settings, keep, crop_anchor, caption_floor
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
        duration = sum(b - a for a, b in keep) if keep else clip.end - clip.start
        visuals, ledger = timed(
            timings, "visuals", prepare_visuals, clip, settings, directory, duration
        )
        write_json(directory / "ledger.json", ledger)
        # Original diagrams carry no on-screen credit; every other visual does.
        credits = sorted(
            {entry["source_credit"] for entry in ledger if entry["kind"] != "diagram"}
        )
        caption = timed(
            timings,
            "captions",
            lambda: create_captions(
                transcript,
                clip,
                directory,
                settings,
                keep,
                credits=credits,
                lifts=layout["lifts"] if layout else None,
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
                keep=keep,
                visuals=visuals,
                **({"chat_ass": directory / "chat.ass"} if settings.chat_panel else {}),
                # Only the chat fill crops by position; standard layouts keep the framing setting.
                **({"crop_x": layout["x"]} if layout and settings.chat_panel else {}),
            ),
        )
        # Checked before the thumbnail, so a strict refusal costs no extra work. The static bands
        # are the ones the captions were placed by, saved in the layout of render.json.
        report = timed(
            timings,
            "quality",
            lambda: quality.record(
                directory,
                quality.check_clip(
                    directory,
                    info,
                    caption["missing_glyphs"],
                    ledger,
                    settings,
                    static=layout.get("static") if layout else None,
                ),
            ),
        )
        quality.enforce(report, settings.quality_gate)
        if settings.thumbnail:
            timed(
                timings,
                "thumbnail",
                thumbnail.make,
                directory / "video.mp4",
                directory / "thumbnail.png",
                clip.title,
                fonts.resolve(settings.font),
                duration / 2,
            )
        names = sorted(
            REQUIRED_FILES
            | {n for n in OPTIONAL_FILES if (directory / n).is_file()}
            | visual_names(ledger)
        )
        saved = {
            "key": render_key,
            "selection": clip.model_dump(),
            "status": "rendered",
            "media": info,
            "captions": caption,
            "quality": report,
            "keep": keep,
            "ledger": ledger,
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


# The transcript depends only on the audio, the recognition settings and the code that reads and
# recognises it, not on captions or rendering. Its own identity lets a saved transcript serve any
# later run, so a change to captions does not repeat the recognition.
TRANSCRIPT_CODE = ("signals.py", "media.py", "asr_split.py")
PARAKEET_CODE = ("parakeet_asr.py", "parakeet_captions.py", "caption_pack.py")


def asr_identity(settings):
    """Recognition hardware that shapes the transcript, without needing that hardware.

    GPU float16 and CPU int8 decode differently, so each keeps its own transcript. An explicit
    device is taken as stated, so a CPU-only later stage can name a GPU transcript; only "auto"
    is resolved on the current machine. CPU results also depend on the thread count.
    """
    device, compute = settings.whisper_device, settings.whisper_compute_type
    if "auto" in (device, compute):
        resolved = asr_options(settings)
        device = resolved["device"] if device == "auto" else device
        compute = resolved["compute_type"] if compute == "auto" else compute
    if device == "cpu" and compute == "int8":
        return {"threads": cpu_threads(settings)}
    return {"whisper_device": device, "whisper_compute_type": compute}


def transcript_key(source, info, settings):
    """The saved transcript to use: a pinned key from a verified analysis, else the identity.

    A downstream stage reuses the transcript its analysis recorded, even after the recognition
    code has changed; a pinned key is only ever read, never recognised again.
    """
    return settings.transcript_key or transcript_identity(source, info, settings)


def transcript_identity(source, info, settings, split=None):
    """split is the cut of a 2-part ASR plan; a different cut is a different transcript."""
    code = {name: digest(Path(__file__).parent / name) for name in TRANSCRIPT_CODE}
    value = {
        "source_sha256": digest(source),
        "duration": info["duration"],
        "audio": info["audio"],
        "audio_start": info.get("audio_start", 0.0),
        "code": code,
        "whisper_model": settings.whisper_model,
        "whisper_revision": settings.whisper_revision,
        "language": settings.language,
        **asr_identity(settings),
        "asr_batch_size": settings.asr_batch_size,
        "asr_parts": settings.asr_parts,
    }
    if settings.asr_engine != "whisper":
        # Whisper keys stay as they were; another recogniser names itself and its code.
        value["asr_engine"] = settings.asr_engine
        value["engine_code"] = {
            name: digest(Path(__file__).parent / name) for name in PARAKEET_CODE
        }
    if split is not None:
        value["split"] = split
    return fingerprint(value)


def saved_transcript(source, info, settings):
    """Read a validated persistent cache only; never invoke ASR or print its text."""
    if not settings.transcript_cache:
        return None
    path = Path(settings.transcript_cache) / (
        transcript_key(source, info, settings) + ".json"
    )
    if path.is_file():
        try:
            return Transcript.model_validate_json(
                path.read_text(encoding="utf-8")
            ).model_dump()
        except (ValueError, UnicodeDecodeError):
            pass
    return None


def recognise(source, info, settings, journal=None):
    """The settings' recogniser: Parakeet, or faster-whisper."""
    if settings.asr_engine == "parakeet":
        from . import parakeet_asr

        return parakeet_asr.transcribe(source, info, settings)
    if journal is None:
        return signals.transcribe(source, info, settings)
    return signals.transcribe(source, info, settings, journal=journal)


def transcribe_once(source, info, settings, receipt=None):
    """Recognised transcript, reused from settings.transcript_cache when its identity matches."""
    receipt = receipt if receipt is not None else {}
    receipt.update(enabled=bool(settings.transcript_cache), reused=False, source="asr")
    if not settings.transcript_cache:
        if settings.asr_parts != 1:
            raise PipelineError("two-part ASR requires the split job coordinator")
        receipt["key"] = None
        return recognise(source, info, settings)
    # The receipt is output metadata, never authority to select another input's cache.
    key = transcript_key(source, info, settings)
    receipt["key"] = key
    path = Path(settings.transcript_cache) / (key + ".json")
    if path.is_file():
        try:
            value = Transcript.model_validate_json(
                path.read_text(encoding="utf-8")
            ).model_dump()
            receipt.update(reused=True, source="transcript_cache")
            return value
        except (ValueError, UnicodeDecodeError):
            # A damaged saved transcript is recognised again rather than used.
            pass
    if settings.require_transcript_cache:
        raise PipelineError(
            "a matching valid transcript cache is required; ASR was not started"
        )
    if settings.asr_parts != 1:
        raise PipelineError("two-part ASR requires the split job coordinator")
    # Segments are noted here as they are recognised; a stopped run keeps them. A new run
    # starts the journal afresh, since recognition is one pass and is not resumed mid-way.
    journal = path.with_suffix(".partial.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal.unlink(missing_ok=True)
    value = recognise(source, info, settings, journal=journal)
    write_json(path, value)
    journal.unlink(missing_ok=True)
    return value
