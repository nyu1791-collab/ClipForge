"""Resumable orchestration with separate proposed, rendered and failed states."""

import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import threading
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

from . import (
    __version__,
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
from . import topics as topic_tools
from .captions import create as create_captions
from .hardware import asr_options, cpu_threads, encode_threads
from .models import Candidate, SelectedClip, Transcript, validate_picks
from .storage import (
    TIME_TOLERANCE,
    PipelineError,
    digest,
    failure_fields,
    fingerprint,
    stage,
    within,
    write_json,
)


def prepare(source, output, resume, settings):
    source = Path(source).resolve(strict=True)
    if (
        not source.is_file()
        or not 0 < source.stat().st_size <= settings.max_input_bytes
    ):
        raise PipelineError(
            "input must be a regular file within the configured size limit"
        )
    requested_output = Path(output).absolute()
    if any(part.is_symlink() for part in [requested_output, *requested_output.parents]):
        raise PipelineError("output directory must not use a symlink")
    output = requested_output.resolve()
    if source.is_relative_to(output):
        raise PipelineError("input video must be outside the output directory")
    if output.exists() and not output.is_dir():
        raise PipelineError("output must be a directory")
    if output.exists() and any(output.iterdir()) and not resume:
        raise PipelineError(
            "output is not empty; use a new directory or explicit --resume"
        )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Refuse symlink aliases before writing any derived artifacts or cache metadata.
    for path in output.rglob("*"):
        if path.is_symlink():
            raise PipelineError("output directory must not contain symlinks")
    lock = (output / ".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise PipelineError("another job is using this output directory") from None
    try:
        implementation = fingerprint(
            {p.name: digest(p) for p in Path(__file__).parent.glob("*.py")}
        )
        identity = {
            "source_sha256": digest(source),
            "settings": settings.model_dump(),
            "version": __version__,
            "implementation": implementation,
        }
        # Render thread budgets do not change clip reuse, even across machines.
        key = fingerprint(
            {**identity, "settings": settings.model_dump(exclude={"encode_threads"})}
        )
        old = output / "input.json"
        if old.exists():
            try:
                saved = json.loads(old.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise PipelineError(
                    "saved input identity is corrupt; choose a new output directory"
                ) from None
            if not isinstance(saved, dict) or saved.get("key") != key:
                raise PipelineError(
                    "resume settings or source differ from this output directory"
                )
        media.require_tools()
        info = media.probe(source)
        if info["duration"] > settings.max_source_seconds:
            raise PipelineError("input exceeds the configured duration limit")
        write_json(old, {"key": key, **identity, "media": info})
    except BaseException:
        lock.close()
        raise
    return source, output, info, key, lock


def verify_source(source, output):
    expected = json.loads((output / "input.json").read_text())["source_sha256"]
    if digest(source) != expected:
        raise PipelineError("input changed during processing; use a stable source file")
    return expected


def start_job(source, output):
    write_json(
        output / "manifest.json",
        {
            "status": "processing",
            "rendered": [],
            "source_sha256": verify_source(source, output),
            "human_review_performed": False,
        },
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


def render_clip(source, root, index, clip, transcript, settings, key, chat=None):
    """Render one clip. chat is {"ass", "messages", "reason", "error"} with the chat panel."""
    name = f"clip-{index:03}"
    target = root / name
    if settings.chat_panel and chat is None:
        raise PipelineError("chat panel needs a Kick VOD URL source")
    render_key = fingerprint(
        {
            "job": key,
            "clip": clip.model_dump(),
            "transcript": transcript.model_dump(),
            # Only chat-panel renders depend on chat; other keys stay as they were.
            **(
                {"chat": {"ass": fingerprint(chat["ass"]), "reason": chat["reason"]}}
                if settings.chat_panel
                else {}
            ),
        }
    )
    receipt = target / "render.json"
    if target.exists():
        if not receipt.is_file():
            raise PipelineError("existing clip has no verified render receipt")
        saved = json.loads(receipt.read_text())
        if not valid_receipt(target, saved, render_key):
            raise PipelineError("existing clip differs from the saved render receipt")
        return {"directory": "clips/" + name, **saved}
    timings = {}
    keep = timed(timings, "silence", cut_regions, source, transcript, clip, settings)
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
                transcript, clip, directory, settings, keep, credits=credits
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
            ),
        )
        # Checked before the thumbnail, so a strict refusal costs no extra work.
        report = timed(
            timings,
            "quality",
            lambda: quality.record(
                directory,
                quality.check_clip(
                    directory, info, caption["missing_glyphs"], ledger, settings
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
        write_json(directory / "render.json", saved)
        # Commit the whole clip directory only when MP4 and captions are complete.
        if target.exists():
            raise PipelineError("clip output already exists")
        directory.rename(target)
    return {"directory": "clips/" + name, **saved}


def run_parallel(function, items, workers):
    """Results in item order. After the first failure, no item that has not started runs."""
    failed = threading.Event()

    def attempt(item):
        if failed.is_set():
            return None
        try:
            return function(item)
        except BaseException:
            failed.set()
            raise

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(attempt, item): index for index, item in enumerate(items)
        }
        try:
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return results


def render_clips(source, output, clips, transcript, settings, key):
    """Render clips in parallel; results keep the selection order."""
    root = within(output, output / "clips")
    root.mkdir(exist_ok=True)
    return run_parallel(
        lambda item: render_clip(
            source, root, item[0], item[1], transcript, settings, key
        ),
        list(enumerate(clips, 1)),
        settings.workers,
    )


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


def transcript_identity(source, info, settings):
    code = {name: digest(Path(__file__).parent / name) for name in TRANSCRIPT_CODE}
    return fingerprint(
        {
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
    )


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


def transcribe_once(source, info, settings, receipt=None):
    """Recognised transcript, reused from settings.transcript_cache when its identity matches."""
    receipt = receipt if receipt is not None else {}
    receipt.update(enabled=bool(settings.transcript_cache), reused=False, source="asr")
    if not settings.transcript_cache:
        if settings.asr_parts != 1:
            raise PipelineError("two-part ASR requires the split job coordinator")
        receipt["key"] = None
        return signals.transcribe(source, info, settings)
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
    value = signals.transcribe(source, info, settings, journal=journal)
    write_json(path, value)
    journal.unlink(missing_ok=True)
    return value


def run(
    source,
    output,
    settings,
    resume=False,
    chat_file=None,
    selection_file=None,
    analyze_only=False,
):
    os.umask(0o077)
    if analyze_only and (Path(output) / "clips").exists():
        raise PipelineError("analysis needs an output directory without rendered clips")
    source, output, info, key, lock = prepare(source, output, resume, settings)
    timings = {}
    current_stage = "initialization"
    try:
        start_job(source, output)
        cache_receipt = {
            "enabled": bool(settings.transcript_cache),
            "key": transcript_key(source, info, settings)
            if settings.transcript_cache
            else None,
            "reused": True,
            "source": "output_stage",
        }
        if (
            settings.require_transcript_cache
            and saved_transcript(source, info, settings) is None
        ):
            raise PipelineError(
                "a matching valid transcript cache is required; ASR was not started"
            )
        current_stage = "transcription"
        print("Transcribing (device chosen from settings and hardware)…", flush=True)
        print("Detecting scene boundaries…", flush=True)
        # Scene detection does not depend on speech, so the two run side by side.
        with ThreadPoolExecutor(max_workers=1) as side:
            scene_future = side.submit(
                timed,
                timings,
                "scene_detection",
                stage,
                output,
                "scenes",
                key,
                lambda: signals.scene_boundaries(source, info["duration"]),
            )
            raw = timed(
                timings,
                "transcription",
                stage,
                output,
                "transcript",
                key,
                lambda: transcribe_once(source, info, settings, cache_receipt),
            )
            transcript = Transcript.model_validate(raw)
            current_stage = "scene_detection"
            scenes = scene_future.result()
        current_stage = "chat_signals"
        supplied = json.loads(Path(chat_file).read_text()) if chat_file else None
        chat_key = fingerprint(
            {"job": key, "file": digest(chat_file) if chat_file else None}
        )
        chat = stage(
            output,
            "chat",
            chat_key,
            lambda: signals.chat_signals(info["duration"], settings, supplied),
        )
        current_stage = "candidate_extraction"
        topic_spans = (
            topic_tools.split(
                transcript,
                info["duration"],
                settings.topic_min_seconds,
                settings.topic_max_seconds,
            )
            if settings.topics
            else None
        )
        candidate_key = fingerprint(
            {"job": key, "transcript": raw, "scenes": scenes, "chat": chat}
        )
        raw_candidates = stage(
            output,
            "candidates",
            candidate_key,
            lambda: signals.candidates(
                info, transcript, scenes, chat, settings, topics=topic_spans
            ),
        )
        candidates = [Candidate.model_validate(c) for c in raw_candidates]
        print(
            f"Constructed {len(candidates)} candidates; chat status: {chat['status']}",
            flush=True,
        )
        if len(candidates) < settings.candidates:
            # Non-overlapping windows may not fit a short or sparse video; say so.
            print(
                f"Only {len(candidates)} of {settings.candidates} requested candidates "
                "fit without overlapping; the video is short or mostly silent.",
                flush=True,
            )

        if analyze_only:
            manifest = {
                "status": "analyzed",
                "source_sha256": verify_source(source, output),
                "version": __version__,
                "settings": settings.model_dump(),
                "clips": [],
                "rendered": [],
                "selection_receipt": {"provider": "none", "generation_requests": 0},
                "topics": topic_spans,
                "candidates": raw_candidates,
                "transcript_cache": cache_receipt,
                "timings": timings,
                "human_review_performed": False,
            }
            write_json(output / "manifest.json", manifest)
            (output / "failure.json").unlink(missing_ok=True)
            return manifest

        def selection():
            if selection_file:
                value = json.loads(Path(selection_file).read_text())
                clips = [SelectedClip.model_validate(c) for c in value["clips"]]
                validate_picks(clips, info["duration"], settings)
                return {
                    "clips": [c.model_dump() for c in clips],
                    "receipt": {
                        "provider": "manual",
                        "generation_requests": 0,
                        "automatic_fallback": False,
                    },
                }
            from .selection import choose

            preview_root = within(output, output / ".analysis")
            preview_root.mkdir(exist_ok=True)
            paths = [
                within(preview_root, preview_root / (candidate.id + ".mp4"))
                for candidate in candidates
            ]

            def make_preview(item):
                candidate, path = item
                result = media.preview(source, path, candidate, settings)
                if abs(result["duration"] - (candidate.end - candidate.start)) > 0.3:
                    raise PipelineError(
                        "analysis preview duration does not match original interval"
                    )

            try:
                run_parallel(
                    make_preview, list(zip(candidates, paths)), settings.workers
                )
                verify_source(source, output)
                print("Requesting one Gemini visual selection…", flush=True)
                return choose(source, paths, candidates, transcript, info, settings)
            finally:
                for path in paths:
                    path.unlink(missing_ok=True)

        current_stage = "selection"
        selection_key = fingerprint(
            {
                "candidates": candidate_key,
                "manual": digest(selection_file) if selection_file else None,
            }
        )
        selected = timed(
            timings, "selection", stage, output, "selection", selection_key, selection
        )
        clips = [SelectedClip.model_validate(c) for c in selected["clips"]]
        validate_picks(clips, info["duration"], settings)
        write_json(
            output / "manifest.json",
            {
                "status": "selected",
                "source_sha256": digest(source),
                "clips": [c.model_dump() for c in clips],
                "rendered": [],
                "selection_receipt": selected["receipt"],
                "topics": topic_spans,
                "transcript_cache": cache_receipt,
                "human_review_performed": False,
            },
        )
        verify_source(source, output)
        current_stage = "rendering"
        print(f"Rendering {len(clips)} selected clips…", flush=True)
        results = timed(
            timings,
            "rendering",
            render_clips,
            source,
            output,
            clips,
            transcript,
            settings,
            key,
        )
        current_stage = "verification"
        manifest = {
            "status": "complete",
            "source_sha256": verify_source(source, output),
            "version": __version__,
            "settings": settings.model_dump(),
            "clips": [c.model_dump() for c in clips],
            "rendered": results,
            "selection_receipt": selected["receipt"],
            "chat_status": chat["status"],
            "topics": topic_spans,
            "candidate_count": {
                "requested": settings.candidates,
                "built": len(candidates),
            },
            "timings": timings,
            "transcript_cache": cache_receipt,
            "human_review_performed": False,
        }
        write_json(output / "manifest.json", manifest)
        (output / "failure.json").unlink(missing_ok=True)
        return manifest
    except Exception as error:
        record_failure(output, error, current_stage)
        raise
    finally:
        lock.close()


def run_batch(inputs, output_root, settings, concurrency, resume=False):
    """Several videos, each in its own folder, with bounded concurrency.

    One failing video does not stop the others; each result is recorded in batch.json.
    """
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = [f"{index:03}-{_folder_name(path)}" for index, path in enumerate(inputs, 1)]
    concurrency = max(1, min(concurrency, len(names)))
    if concurrency > 1 and settings.encode_threads == 0:
        # Share available render cores across the videos actually running at once.
        settings = settings.model_copy(
            update={"encode_threads": max(1, encode_threads(settings) // concurrency)}
        )

    def one(item):
        source, name = item
        try:
            manifest = run(source, root / name, settings, resume)
            return {
                "folder": name,
                "status": manifest["status"],
                "rendered": len(manifest["rendered"]),
            }
        except Exception as error:
            detail = (
                str(error) if isinstance(error, PipelineError) else type(error).__name__
            )
            return {"folder": name, "status": "failed", "error": detail[:240]}

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, zip(inputs, names)))
    write_json(
        root / "batch.json", {"results": results, "human_review_performed": False}
    )
    return results


def default_concurrency(settings):
    """Videos processed at once: enough cores for each video's threads and render workers."""
    cores = os.cpu_count() or 1
    return max(1, cores // max(1, cpu_threads(settings) * settings.workers))


def _folder_name(path):
    stem = re.sub(r"[^\w-]", "_", Path(path).stem)[:40].strip("._")
    return stem or "video"


def cut(source, output, start, end, settings, transcript_file=None, resume=False):
    os.umask(0o077)
    source, output, info, key, lock = prepare(source, output, resume, settings)
    current_stage = "clip_validation"
    try:
        start_job(source, output)
        clip = SelectedClip(
            start=start,
            end=end,
            candidate_id="manual",
            title="Manual clip",
            reason="User-selected interval",
            confidence=1,
        )
        if end > info["duration"] + TIME_TOLERANCE:
            raise PipelineError("clip interval exceeds the input video")
        transcript = (
            Transcript.model_validate_json(Path(transcript_file).read_text())
            if transcript_file
            else Transcript(language="und", segments=[])
        )
        current_stage = "rendering"
        results = render_clips(source, output, [clip], transcript, settings, key)
        current_stage = "verification"
        value = {
            "status": "complete",
            "source_sha256": verify_source(source, output),
            "rendered": results,
            "selection_receipt": {"provider": "manual", "generation_requests": 0},
            "human_review_performed": False,
        }
        write_json(output / "manifest.json", value)
        (output / "failure.json").unlink(missing_ok=True)
        return value
    except Exception as error:
        record_failure(output, error, current_stage)
        raise
    finally:
        lock.close()
