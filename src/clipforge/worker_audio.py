"""Private CPU worker stages reuse one verified ASR artifact; no model API calls."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from . import audio_first, landscape, media, pipeline
from .github_cli import run as run_gh
from .kick_media import KickVOD, canonical
from .models import Settings, Transcript
from .storage import PipelineError, digest, process, within, write_json

REPO = "nyu1791-collab/ai-model-workers"
PART_RUN = re.compile(r"([0-9]{1,20}):([a-f0-9]{32})")


def gh(*args):
    return run_gh(
        *args,
        timeout=300,
        error_type=PipelineError,
        message="private analysis artifact operation failed",
    )


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


def execute(
    previous, output, previous_id, settings, mode, review_file=None, clip_index=None
):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with audio_first.exclusive(output):
        url = restore_analysis(previous, output, previous_id)
        # The verified analysis names its transcript; later code changes to recognition do
        # not orphan it, and no new recognition can start from here.
        recorded = json.loads((output / "asr-receipt.json").read_text())["key"]
        settings = settings.model_copy(
            update={
                "transcript_cache": str(output / "transcripts"),
                "require_transcript_cache": True,
                "transcript_key": recorded,
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
                    vod,
                    output,
                    settings,
                    review_file,
                    analysis_result=analysis,
                    only=clip_index,
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


def clip_index_argument(text):
    value = int(text)
    if not 1 <= value <= 20:
        raise argparse.ArgumentTypeError("clip index must be 1-20")
    return value


def private_member(root, name):
    """A receipt name must be a plain relative path to a regular file inside its part."""
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name:
        raise PipelineError("private part names must be plain relative paths")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise PipelineError("private part contains a symbolic link")
    target = within(root, current)
    if not target.is_file():
        raise PipelineError("private part file is missing")
    return target


def load_part(root, expected_request, source_ref):
    """Verify one single-clip render before any of its files are used."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise PipelineError("private clip part is not a directory")
    receipt = json.loads(private_member(root, "request-receipt.json").read_text())
    manifest = json.loads(private_member(root, "manifest.json").read_text())
    hashes = receipt.get("sha256")
    if not isinstance(hashes, dict) or not hashes or len(hashes) > 200:
        raise PipelineError("private clip part receipts are incomplete")
    for name, sha in hashes.items():
        if not isinstance(sha, str) or digest(private_member(root, name)) != sha:
            raise PipelineError("private clip part files changed")
    required = {"manifest.json", "asr-receipt.json", "delivery/manifest.json"}
    index = receipt.get("clip_index")
    clip_count = manifest.get("clip_count")
    request_id = receipt.get("request_id")
    if (
        not required <= set(hashes)
        or receipt.get("status") != "clip_rendered"
        or manifest.get("status") != "clip_rendered"
        or type(index) is not int
        or not 1 <= index <= 20
        or manifest.get("clip_index") != index
        or type(clip_count) is not int
        or not isinstance(request_id, str)
        or not re.fullmatch(r"[a-f0-9]{32}", request_id)
        or (expected_request is not None and request_id != expected_request)
        or receipt.get("source_ref") != source_ref
        or receipt.get("asr_reused") is not True
        or manifest.get("model_generation_requests") != 0
    ):
        raise PipelineError("private clip part identity or completion state mismatch")
    name = f"clip-{index:03}"
    rendered = manifest.get("rendered")
    timings = manifest.get("timings")
    if (
        not isinstance(rendered, list)
        or len(rendered) != 1
        or rendered[0].get("directory") != "delivery/clips/" + name
        or not isinstance(timings, dict)
        or not isinstance(timings.get(name), dict)
        or "total" not in timings
        or any(
            entry.startswith("delivery/clips/")
            and not entry.startswith(f"delivery/clips/{name}/")
            for entry in hashes
        )
    ):
        raise PipelineError("private clip part does not hold exactly its own clip")
    cache = json.loads(private_member(root, "asr-receipt.json").read_text())
    identity = {
        "previous_request_id": receipt.get("previous_request_id"),
        "analysis_key": manifest.get("analysis_key"),
        "selection_sha256": manifest.get("selection_sha256"),
        "audio_source_url": receipt.get("audio_source_url"),
        "source_sha256": manifest.get("source_sha256"),
        "clip_count": clip_count,
        "transcript_key": cache.get("key"),
    }
    return {
        "root": root,
        "index": index,
        "request_id": request_id,
        "hashes": hashes,
        "rendered": rendered,
        "timings": timings,
        "cache": cache,
        "identity": identity,
    }


def merge_parts(parts, output, settings, source_ref):
    """Join verified single-clip renders into the delivery a whole-video render writes."""
    loaded = [load_part(path, request, source_ref) for path, request in parts]
    identity = loaded[0]["identity"]
    if any(item["identity"] != identity for item in loaded):
        raise PipelineError("clip renders belong to different analyses or selections")
    if identity["clip_count"] != settings.clips:
        raise PipelineError("clip count does not match the plan")
    by_index = {}
    for item in loaded:
        if item["index"] in by_index:
            raise PipelineError("duplicate clip render")
        by_index[item["index"]] = item
    if sorted(by_index) != list(range(1, settings.clips + 1)):
        raise PipelineError("clip renders must cover every reviewed clip exactly once")
    if (output / "delivery").exists():
        raise PipelineError("merge output already contains a delivery")
    delivery = output / "delivery"
    rows, timings = [], {}
    for index in range(1, settings.clips + 1):
        item = by_index[index]
        name = f"clip-{index:03}"
        for entry in item["hashes"]:
            if entry.startswith(f"delivery/clips/{name}/"):
                target = within(output, output / entry)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(private_member(item["root"], entry), target)
        rows.append(dict(item["rendered"][0], directory="clips/" + name))
        timings[name] = item["timings"][name]
    timings["total"] = round(
        sum(v for t in timings.values() if isinstance(t, dict) for v in t.values()), 2
    )
    manifest = {
        "status": "complete",
        "rendered": rows,
        "selection_receipt": {"provider": "manual", "generation_requests": 0},
        "analysis_key": identity["analysis_key"],
        "selection_sha256": identity["selection_sha256"],
        "video_review_attested": True,
        "human_review_performed": False,
        "transcript_cache": loaded[0]["cache"],
        "timings": timings,
    }
    summary = landscape.summarize_delivery(manifest, delivery, settings.clips)
    summary["topic_content_review"] = "attested_in_selection_file"
    summary["reproducibility"] = landscape.code_identity(settings)
    write_json(delivery / "manifest.json", manifest)
    write_json(delivery / "e2e-summary.json", summary)
    result = {
        **manifest,
        "rendered": [
            dict(row, directory="delivery/" + row["directory"]) for row in rows
        ],
        "source_url": identity["audio_source_url"],
        "source_sha256": identity["source_sha256"],
        "audio_stage": "render",
        "asr_reused": True,
        "human_review_performed": False,
        "model_generation_requests": 0,
    }
    write_json(output / "manifest.json", result)
    lineage = {
        "previous_request_id": identity["previous_request_id"],
        "part_request_ids": sorted(item["request_id"] for item in loaded),
    }
    return result, lineage


def download_parts(text, directory):
    """Fetch each clip render only after its run is confirmed as that request's success."""
    parts, seen = [], set()
    for entry in filter(None, text.split(",")):
        match = PART_RUN.fullmatch(entry)
        if match is None:
            raise PipelineError("invalid clip render run identity")
        run_id, request = match.groups()
        if request in seen:
            raise PipelineError("duplicate clip render request")
        seen.add(request)
        run = json.loads(
            gh(
                "run",
                "view",
                run_id,
                "--repo",
                REPO,
                "--json",
                "status,conclusion,displayTitle",
            )
        )
        if (
            run["status"] != "completed"
            or run["conclusion"] != "success"
            or run["displayTitle"] != "ClipForge " + request
        ):
            raise PipelineError("clip render did not complete successfully")
        destination = directory / request
        gh(
            "run",
            "download",
            run_id,
            "--repo",
            REPO,
            "--name",
            "clipforge-result-" + request,
            "--dir",
            str(destination),
        )
        parts.append((destination, request))
    return parts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", choices=("previews", "render", "merge"), required=True
    )
    parser.add_argument("--previous-run")
    parser.add_argument("--previous-request")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/audio-stage"))
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--clip-index", type=clip_index_argument)
    parser.add_argument("--part-dir", type=Path, action="append", default=[])
    parser.add_argument("--part-runs", default="")
    parser.add_argument(
        "--config", type=Path, default=Path("config.landscape.example.json")
    )
    args = parser.parse_args(argv)
    try:
        os.umask(0o077)
        if (
            os.environ.get("GITHUB_REPOSITORY") != REPO
            or not re.fullmatch(r"[a-f0-9]{32}", args.request_id)
            or not re.fullmatch(r"[a-f0-9]{40}", args.source_ref)
            or (args.clip_index is not None and args.stage != "render")
            or (args.stage == "merge" and not (args.part_dir or args.part_runs))
            or (args.stage != "merge" and (args.part_dir or args.part_runs))
        ):
            raise PipelineError("invalid private worker identity")
        if args.stage != "merge" and (
            not re.fullmatch(r"[0-9]{1,20}", args.previous_run or "")
            or not re.fullmatch(r"[a-f0-9]{32}", args.previous_request or "")
        ):
            raise PipelineError("invalid private worker identity")
        settings = Settings.model_validate_json(args.config.read_text())
        if (settings.aspect, settings.height, settings.quality_gate) != (
            "16:9",
            1080,
            "strict",
        ):
            raise PipelineError("horizontal worker settings are required")
        if args.stage == "merge":
            with tempfile.TemporaryDirectory(prefix="clipforge-parts-") as temporary:
                parts = [(path, None) for path in args.part_dir]
                parts += download_parts(args.part_runs, Path(temporary))
                result, lineage = merge_parts(
                    parts, args.output, settings, args.source_ref
                )
        else:
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
                    args.clip_index,
                )
            lineage = {
                "previous_run_id": args.previous_run,
                "previous_request_id": args.previous_request,
            }
        hashes = {
            str(p.relative_to(args.output)): digest(p)
            for p in args.output.rglob("*")
            if p.is_file()
            and not any(
                part.startswith(".") for part in p.relative_to(args.output).parts
            )
            and p.name != "request-receipt.json"
        }
        receipt = {
            "request_id": args.request_id,
            "source_ref": args.source_ref,
            "workflow_run": os.environ.get("GITHUB_RUN_ID"),
            "status": result["status"],
            "audio_source_url": result["source_url"],
            "sha256": hashes,
            "asr_reused": True,
            "audio_stage": args.stage,
            **lineage,
        }
        if args.clip_index is not None:
            receipt["clip_index"] = args.clip_index
        write_json(args.output / "request-receipt.json", receipt)
        summary = {
            "status": result["status"],
            "asr_reused": True,
            "model_generation_requests": 0,
        }
        if "timings" in result:
            summary["timings"] = result["timings"]
        print(json.dumps(summary))
        return 0
    except (OSError, ValueError, KeyError, TypeError, PipelineError) as error:
        # Only PipelineError text is our own fixed wording; other exceptions keep just the type.
        message = str(error) if isinstance(error, PipelineError) else ""
        record = {
            "stage": args.stage,
            "error_type": type(error).__name__,
            "message": message,
        }
        with contextlib.suppress(OSError):
            write_json(args.output / "stage-error.json", record)
        line = "Private audio stage failed: " + type(error).__name__
        if message:
            line += ": " + message
        print(line, file=sys.stderr)
        print(
            "Private audio stage failed; inspect its private artifact. No ASR or model fallback was requested.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
