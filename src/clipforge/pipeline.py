"""Resumable orchestration with separate proposed, rendered and failed states."""

import fcntl
import json
import os
from pathlib import Path
import tempfile

from . import __version__, media, signals
from .captions import create as create_captions
from .models import Candidate, SelectedClip, Transcript, validate_picks
from .storage import PipelineError, digest, fingerprint, stage, within, write_json


def prepare(source, output, resume, settings):
    source = Path(source).resolve(strict=True)
    if not source.is_file() or source.stat().st_size > settings.max_input_bytes:
        raise PipelineError(
            "input must be a regular file within the configured size limit"
        )
    output = Path(output).resolve()
    if source.is_relative_to(output):
        raise PipelineError("input video must be outside the output directory")
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
    implementation = fingerprint(
        {p.name: digest(p) for p in Path(__file__).parent.glob("*.py")}
    )
    identity = {
        "source_sha256": digest(source),
        "settings": settings.model_dump(),
        "version": __version__,
        "implementation": implementation,
    }
    key = fingerprint(identity)
    old = output / "input.json"
    if old.exists() and json.loads(old.read_text()).get("key") != key:
        lock.close()
        raise PipelineError(
            "resume settings or source differ from this output directory"
        )
    try:
        media.require_tools()
        info = media.probe(source)
        if info["duration"] > settings.max_source_seconds:
            raise PipelineError("input exceeds the configured duration limit")
        write_json(old, {"key": key, **identity, "media": info})
    except BaseException:
        lock.close()
        raise
    return source, output, info, key, lock


def render_clips(source, output, clips, transcript, settings, key):
    root = within(output, output / "clips")
    root.mkdir(exist_ok=True)
    rendered = []
    for index, clip in enumerate(clips, 1):
        name = f"clip-{index:03}"
        target = root / name
        render_key = fingerprint(
            {
                "job": key,
                "clip": clip.model_dump(),
                "transcript": transcript.model_dump(),
            }
        )
        receipt = target / "render.json"
        if target.exists():
            if not receipt.is_file():
                raise PipelineError("existing clip has no verified render receipt")
            old = json.loads(receipt.read_text())
            if (
                old.get("key") != render_key
                or any(
                    not (target / n).is_file() or digest(target / n) != sha
                    for n, sha in old.get("sha256", {}).items()
                )
                or set(old.get("sha256", {}))
                != {"video.mp4", "captions.ass", "captions.srt"}
            ):
                raise PipelineError(
                    "existing clip differs from the saved render receipt"
                )
            rendered.append({"directory": "clips/" + name, **old})
            continue
        with tempfile.TemporaryDirectory(
            prefix="." + name + "-", dir=root
        ) as temporary:
            directory = Path(temporary)
            caption = create_captions(transcript, clip, directory, settings)
            info = media.render(
                source,
                directory / "video.mp4",
                clip,
                settings,
                ass=directory / "captions.ass",
            )
            old = {
                "key": render_key,
                "selection": clip.model_dump(),
                "status": "rendered",
                "media": info,
                "captions": caption,
                "sha256": {
                    n: digest(directory / n)
                    for n in ["video.mp4", "captions.ass", "captions.srt"]
                },
            }
            write_json(directory / "render.json", old)
            # Commit the whole clip directory only when MP4 and captions are complete.
            if target.exists():
                raise PipelineError("clip output already exists")
            directory.rename(target)
        rendered.append({"directory": "clips/" + name, **old})
    return rendered


def run(source, output, settings, resume=False, chat_file=None, selection_file=None):
    os.umask(0o077)
    source, output, info, key, lock = prepare(source, output, resume, settings)
    try:
        print("Transcribing on CPU…", flush=True)
        raw = stage(
            output,
            "transcript",
            key,
            lambda: signals.transcribe(source, info, settings),
        )
        transcript = Transcript.model_validate(raw)
        print("Detecting scene boundaries…", flush=True)
        scenes = stage(
            output,
            "scenes",
            key,
            lambda: signals.scene_boundaries(source, info["duration"]),
        )
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
        candidate_key = fingerprint(
            {"job": key, "transcript": raw, "scenes": scenes, "chat": chat}
        )
        raw_candidates = stage(
            output,
            "candidates",
            candidate_key,
            lambda: signals.candidates(info, transcript, scenes, chat, settings),
        )
        candidates = [Candidate.model_validate(c) for c in raw_candidates]
        print(
            f"Constructed {len(candidates)} candidates; chat status: {chat['status']}",
            flush=True,
        )

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
            paths = []
            for candidate in candidates:
                path = preview_root / (candidate.id + ".mp4")
                result = media.preview(source, path, candidate, settings)
                if abs(result["duration"] - (candidate.end - candidate.start)) > 0.3:
                    raise PipelineError(
                        "analysis preview duration does not match original interval"
                    )
                paths.append(path)
            print("Requesting one Gemini visual selection…", flush=True)
            try:
                return choose(source, paths, candidates, transcript, info, settings)
            finally:
                for path in paths:
                    path.unlink(missing_ok=True)

        selection_key = fingerprint(
            {
                "candidates": candidate_key,
                "manual": digest(selection_file) if selection_file else None,
            }
        )
        selected = stage(output, "selection", selection_key, selection)
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
            },
        )
        print(f"Rendering {len(clips)} selected clips…", flush=True)
        results = render_clips(source, output, clips, transcript, settings, key)
        manifest = {
            "status": "complete",
            "source_sha256": digest(source),
            "version": __version__,
            "settings": settings.model_dump(),
            "clips": [c.model_dump() for c in clips],
            "rendered": results,
            "selection_receipt": selected["receipt"],
            "chat_status": chat["status"],
            "human_review_performed": False,
        }
        write_json(output / "manifest.json", manifest)
        (output / "failure.json").unlink(missing_ok=True)
        return manifest
    except Exception as error:
        write_json(
            output / "failure.json",
            {
                "status": "failed",
                "error_type": type(error).__name__,
                "complete": False,
                "details_omitted": True,
            },
        )
        raise
    finally:
        lock.close()


def cut(source, output, start, end, settings, transcript_file=None, resume=False):
    os.umask(0o077)
    source, output, info, key, lock = prepare(source, output, resume, settings)
    try:
        clip = SelectedClip(
            start=start,
            end=end,
            candidate_id="manual",
            title="Manual clip",
            reason="User-selected interval",
            confidence=1,
        )
        if end > info["duration"] + 0.01:
            raise PipelineError("clip interval exceeds the input video")
        transcript = (
            Transcript.model_validate_json(Path(transcript_file).read_text())
            if transcript_file
            else Transcript(language="und", segments=[])
        )
        results = render_clips(source, output, [clip], transcript, settings, key)
        value = {
            "status": "complete",
            "source_sha256": digest(source),
            "rendered": results,
            "selection_receipt": {"provider": "manual", "generation_requests": 0},
            "human_review_performed": False,
        }
        write_json(output / "manifest.json", value)
        return value
    finally:
        lock.close()
