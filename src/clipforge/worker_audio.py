"""Private CPU worker stages reuse one verified ASR artifact; no model API calls."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from . import audio_first, media, pipeline
from .kick_media import KickVOD, canonical
from .models import Settings, Transcript
from .storage import PipelineError, digest, process, within, write_json

REPO = "nyu1791-collab/ai-model-workers"


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise PipelineError("private analysis artifact operation failed")
    return result.stdout


def restore_analysis(previous, output, previous_id):
    receipt = json.loads((previous / "request-receipt.json").read_text())
    manifest = json.loads((previous / "manifest.json").read_text())
    if (
        receipt.get("request_id") != previous_id
        or receipt.get("status") not in {"topics_allocated", "awaiting_video_review"}
        or manifest.get("status") != receipt["status"]
        or manifest.get("model_generation_requests") != 0
    ):
        raise PipelineError("previous analysis identity or completion state mismatch")
    url = canonical(manifest["source_url"])
    if receipt.get("audio_source_url") != url:
        raise PipelineError("previous audio source identity mismatch")
    cache = json.loads((previous / "asr-receipt.json").read_text())
    key = cache.get("key", "")
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise PipelineError("previous ASR identity missing")
    required = {
        "audio.m4a",
        "manifest.json",
        "topics.json",
        "asr-receipt.json",
        "transcripts/" + key + ".json",
    }
    hashes = receipt.get("sha256", {})
    if not required <= set(hashes) or len(hashes) > 200:
        raise PipelineError("previous analysis receipts are incomplete")
    for name, sha in hashes.items():
        path = within(previous, previous / name)
        if path.is_symlink() or digest(path) != sha:
            raise PipelineError("previous analysis files changed")
    if digest(previous / "audio.m4a") != manifest.get("source_sha256"):
        raise PipelineError("previous audio checksum mismatch")
    Transcript.model_validate_json(
        (previous / "transcripts" / (key + ".json")).read_text()
    )
    for name in hashes:
        allowed = (
            name in required - {"manifest.json"}
            or name == "previews.json"
            or re.fullmatch(r"previews/topic-[0-9]{3}\.mp4(?:\.receipt\.json)?", name)
        )
        if allowed:
            target = within(output, output / name)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() and digest(target) != hashes[name]:
                raise PipelineError("existing worker input differs")
            if not target.exists():
                shutil.copyfile(previous / name, target)
    return url


def clock_text(seconds):
    """Format seconds as M:SS, or H:MM:SS from one hour on (floored)."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes}:{secs:02}"


def sample_label(topic_id, start, offset):
    """Stream time when the candidate start is known, otherwise seconds inside it."""
    if start is None:
        return f"{topic_id} @ +{clock_text(offset)}"
    return f"{topic_id} @ {clock_text(start + offset)}"


def candidate_starts(root, previews):
    """Map topic_id to its stream start, from previews.json receipts or topics.json."""
    starts = {}
    topics_path = root / "topics.json"
    if topics_path.is_file():
        for topic in json.loads(topics_path.read_text()).get("topics", []):
            starts[topic["id"]] = topic["start"]
    for row in previews["previews"]:
        value = (row.get("receipt") or {}).get("source_start")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            starts[row["topic_id"]] = float(value)
    return starts


def preview_contact_sheet(root, previews):
    """Three labelled samples per candidate; this does not attest full-video viewing."""
    from PIL import Image, ImageDraw

    starts = candidate_starts(root, previews)
    rows = []
    for row in previews["previews"]:
        source = within(root, root / row["file"])
        info = media.probe(source)
        panel = Image.new("RGB", (960, 208), "black")
        draw = ImageDraw.Draw(panel)
        draw.text((8, 5), row["topic_id"] + " : 10%, 50%, 90%", fill="white")
        with tempfile.TemporaryDirectory(prefix="clipforge-frames-") as temporary:
            for index, fraction in enumerate((0.1, 0.5, 0.9)):
                target = Path(temporary) / f"{index}.jpg"
                process(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-ss",
                        str(info["duration"] * fraction),
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        source,
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
                        target,
                    ]
                )
                with Image.open(target) as frame:
                    panel.paste(frame.convert("RGB"), (320 * index, 28))
                offset = info["duration"] * fraction
                label = sample_label(
                    row["topic_id"], starts.get(row["topic_id"]), offset
                )
                left, top, right, bottom = draw.textbbox((0, 0), label)
                x, y = 320 * index + 4, 28 + 4
                draw.rectangle(
                    (x - 2, y - 2, x + (right - left) + 2, y + (bottom - top) + 2),
                    fill="black",
                )
                draw.text((x, y), label, fill="white")
        rows.append(panel)
    if not rows:
        raise PipelineError("no preview candidates to inspect")
    sheet = Image.new("RGB", (960, 208 * len(rows)), "black")
    for index, row in enumerate(rows):
        sheet.paste(row, (0, 208 * index))
    sheet.save(root / "preview-contact-sheet.jpg", quality=90)


def execute(previous, output, previous_id, settings, mode, review_file=None):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with audio_first.exclusive(output):
        url = restore_analysis(previous, output, previous_id)
        settings = settings.model_copy(
            update={
                "transcript_cache": str(output / "transcripts"),
                "require_transcript_cache": True,
            }
        )
        source = output / "audio.m4a"
        info = audio_first.audio_info(source)
        if pipeline.saved_transcript(source, info, settings) is None:
            raise PipelineError(
                "the saved ASR does not match; no new recognition was started"
            )
        analysis = audio_first.allocate_audio(source, output, settings, url)
        if not json.loads((output / "asr-receipt.json").read_text())["reused"]:
            raise PipelineError("downstream stage must reuse ASR")
        vod = KickVOD(
            url,
            output / "segments",
            max_bytes=2 * 1024**3,
            max_seconds=settings.max_source_seconds,
        )
        try:
            if mode == "previews":
                selection = (
                    json.loads(review_file.read_text())
                    if review_file is not None
                    else {}
                )
                options = (
                    {"topic_ids": selection["topic_ids"]}
                    if "topic_ids" in selection
                    else {}
                )
                result = audio_first.previews(
                    vod, output, settings, analysis_result=analysis, **options
                )
                preview_contact_sheet(output, result)
                result.update(rendered=[], model_generation_requests=0)
            elif mode == "render" and review_file is not None:
                audio_first.render(
                    vod, output, settings, review_file, analysis_result=analysis
                )
                result = json.loads((output / "delivery/manifest.json").read_text())
                for row in result["rendered"]:
                    row["directory"] = "delivery/" + row["directory"]
                result["model_generation_requests"] = 0
            else:
                raise PipelineError("invalid audio worker stage or missing review")
        finally:
            audio_first.discard_transport_cache(vod)
            vod.close()
        result.update(
            source_url=url,
            source_sha256=digest(source),
            audio_stage=mode,
            asr_reused=True,
            human_review_performed=False,
        )
        write_json(output / "manifest.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("previews", "render"), required=True)
    parser.add_argument("--previous-run", required=True)
    parser.add_argument("--previous-request", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/audio-stage"))
    parser.add_argument("--review-file", type=Path)
    parser.add_argument(
        "--config", type=Path, default=Path("config.landscape.example.json")
    )
    args = parser.parse_args(argv)
    try:
        os.umask(0o077)
        if (
            os.environ.get("GITHUB_REPOSITORY") != REPO
            or not re.fullmatch(r"[0-9]{1,20}", args.previous_run)
            or not re.fullmatch(r"[a-f0-9]{32}", args.previous_request)
            or not re.fullmatch(r"[a-f0-9]{32}", args.request_id)
            or not re.fullmatch(r"[a-f0-9]{40}", args.source_ref)
        ):
            raise PipelineError("invalid private worker identity")
        run = json.loads(
            gh(
                "run",
                "view",
                args.previous_run,
                "--repo",
                REPO,
                "--json",
                "status,conclusion,displayTitle",
            )
        )
        if (
            run["status"] != "completed"
            or run["conclusion"] != "success"
            or run["displayTitle"] != "ClipForge " + args.previous_request
        ):
            raise PipelineError("previous analysis did not complete successfully")
        settings = Settings.model_validate_json(args.config.read_text())
        if (settings.aspect, settings.height, settings.quality_gate) != (
            "16:9",
            1080,
            "strict",
        ):
            raise PipelineError("horizontal worker settings are required")
        with tempfile.TemporaryDirectory(prefix="clipforge-previous-") as temporary:
            previous = Path(temporary) / "analysis"
            gh(
                "run",
                "download",
                args.previous_run,
                "--repo",
                REPO,
                "--name",
                "clipforge-result-" + args.previous_request,
                "--dir",
                str(previous),
            )
            result = execute(
                previous,
                args.output,
                args.previous_request,
                settings,
                args.stage,
                args.review_file,
            )
        hashes = {
            str(p.relative_to(args.output)): digest(p)
            for p in args.output.rglob("*")
            if p.is_file()
            and not any(
                part.startswith(".") for part in p.relative_to(args.output).parts
            )
            and p.name != "request-receipt.json"
        }
        write_json(
            args.output / "request-receipt.json",
            {
                "request_id": args.request_id,
                "source_ref": args.source_ref,
                "workflow_run": os.environ.get("GITHUB_RUN_ID"),
                "status": result["status"],
                "audio_source_url": result["source_url"],
                "sha256": hashes,
                "asr_reused": True,
                "audio_stage": args.stage,
                "previous_run_id": args.previous_run,
                "previous_request_id": args.previous_request,
            },
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "asr_reused": True,
                    "model_generation_requests": 0,
                }
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, PipelineError):
        print(
            "Private audio stage failed; inspect its private artifact. No ASR or model fallback was requested.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
