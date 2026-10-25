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

from . import (
    audio_first,
    audio_volume,
    landscape,
    media,
    pipeline,
    review_draft,
    topic_hints,
)
from .github_cli import run as run_gh
from .kick_media import KickVOD
from .media_sources import canonical, open_source
from .models import Settings, Transcript
from .storage import (
    PipelineError,
    digest,
    failure_fields,
    process,
    within,
    write_json,
)

REPO = "nyu1791-collab/ai-model-workers"
PART_RUN = re.compile(r"([0-9]{1,20}):([a-f0-9]{32})")
RUN_FIELDS = "status,conclusion,displayTitle,event,workflowName,headBranch"
STAGE_WORKFLOWS = {"ClipForge audio-first stages", "ClipForge"}
STAGE_BRANCHES = {"main"}
# Configuration files at the repository root; receipts without the field predate the choice.
DEFAULT_CONFIG_FILE = "config.landscape.example.json"
CONFIG_FILE_PATTERN = r"config\.[a-z0-9-]+\.example\.json"


def gh(*args):
    return run_gh(
        *args,
        timeout=300,
        error_type=PipelineError,
        message="private analysis artifact operation failed",
    )


def successful_stage_run(run, request):
    return (
        run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("displayTitle") == "ClipForge " + request
        and run.get("event") == "workflow_dispatch"
        and run.get("workflowName") in STAGE_WORKFLOWS
        and run.get("headBranch") in STAGE_BRANCHES
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
                    ],
                    stage="frames",
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


# How the analysis produced its transcript, carried into every later stage's receipt.
ORIGIN_FIELDS = (
    "source",
    "parts",
    "overlap_seconds",
    "part_requests",
    "split",
    "split_key",
)


def _hint_ranking(analysis, volume):
    """Topic IDs best first by the text, speech and loudness hints; empty if they fail."""
    try:
        ranked = topic_hints.hint_topics(analysis, volume=volume)["topics"]
    except (ValueError, KeyError, TypeError):
        return []
    return [row["id"] for row in ranked]


def _volume_series(source, info):
    """(series, reason): per-second loudness on the topic clock, or None and a reason code."""
    try:
        return audio_volume.loudness_series(source, offset=info["audio_start"]), None
    except audio_volume.VolumeUnavailable as error:
        return None, error.reason


def execute(
    previous, output, previous_id, settings, mode, review_file=None, clip_index=None
):
    if mode == "render":
        settings = settings.model_copy(
            update={
                "video_encoder": "h264_nvenc",
                "loudness_two_pass": False,
                "height": 360,
                "chat_panel_width": round(
                    settings.chat_panel_width * 360 / settings.height / 2
                )
                * 2,
            }
        )
        # Fail before downloading analysis/media if this GPU cannot actually encode.
        from .hardware import video_options

        video_options(settings, 18)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with audio_first.exclusive(output):
        url = restore_analysis(previous, output, previous_id)
        # The verified analysis names its transcript; later code changes to recognition do
        # not orphan it, and no new recognition can start from here.
        origin = json.loads((output / "asr-receipt.json").read_text())
        recorded = origin["key"]
        settings = settings.model_copy(
            update={
                "transcript_cache": str(output / "transcripts"),
                "require_transcript_cache": True,
                "transcript_key": recorded,
            }
        )
        source = output / "audio.m4a"
        info = audio_first.audio_info(source)
        if pipeline.saved_transcript(settings) is None:
            raise PipelineError(
                "the saved ASR does not match; no new recognition was started"
            )
        analysis = audio_first.allocate_audio(source, output, settings, url)
        cache = json.loads((output / "asr-receipt.json").read_text())
        if not cache["reused"]:
            raise PipelineError("downstream stage must reuse ASR")
        # Reuse rewrites the receipt; keep how the analysis recognised it, such as the cut.
        cache["origin"] = origin.get("origin") or {
            name: origin[name] for name in ORIGIN_FIELDS if name in origin
        }
        write_json(output / "asr-receipt.json", cache)
        vod = open_source(
            url,
            output / "segments",
            kick_factory=KickVOD,
            # The same transport bound as acquisition, so a later stage never refuses
            # input that an earlier stage accepted.
            max_bytes=settings.max_transport_bytes,
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
                volume, volume_reason = _volume_series(source, info)
                # Rank by the hints that need no video, so the previewed candidates are
                # the strongest ones rather than the longest-talking ones.
                ranking = _hint_ranking(analysis[0], volume)
                result = audio_first.previews(
                    vod,
                    output,
                    settings,
                    analysis_result=analysis,
                    ranking=ranking,
                    **options,
                )
                from .chat_activity import candidate_series

                chat, chat_diagnostics = candidate_series(vod, analysis[0], result)
                draft, diagnostics = review_draft.build_review_draft(
                    analysis[0],
                    result,
                    settings,
                    transcript=analysis[1],
                    volume=volume,
                    volume_reason=volume_reason,
                    chat=chat,
                    chat_diagnostics=chat_diagnostics,
                )
                write_json(output / "review-draft.json", draft)
                write_json(output / "review-draft-diagnostics.json", diagnostics)
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
        if mode == "render":
            # Final deliveries retain clips and receipts, not input media or ASR bodies.
            for name in ("ranges", "previews", "transcripts", ".state"):
                if (output / name).exists():
                    shutil.rmtree(output / name)
            for name in (
                "audio.m4a",
                "audio.m4a.receipt.json",
                "previews.json",
                "topics.json",
            ):
                (output / name).unlink(missing_ok=True)
        return result


def clip_index_argument(text):
    value = int(text)
    if not 1 <= value <= 20:
        raise argparse.ArgumentTypeError("clip index must be 1-20")
    return value


def receipt_config_file(root):
    """The configuration file a private part was rendered with."""
    receipt = json.loads(private_member(root, "request-receipt.json").read_text())
    name = receipt.get("config_file", DEFAULT_CONFIG_FILE)
    if not isinstance(name, str) or not re.fullmatch(CONFIG_FILE_PATTERN, name):
        raise PipelineError("private clip part configuration is invalid")
    return name


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
    config_file = receipt_config_file(root)
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
        "config_file": config_file,
    }


def merge_parts(parts, output, settings, source_ref, config_file=None):
    """Join verified single-clip renders into the delivery a whole-video render writes."""
    loaded = [load_part(path, request, source_ref) for path, request in parts]
    identity = loaded[0]["identity"]
    if any(item["identity"] != identity for item in loaded):
        raise PipelineError("clip renders belong to different analyses or selections")
    part_configs = {item["config_file"] for item in loaded}
    if len(part_configs) != 1:
        raise PipelineError("clip renders use different configuration files")
    if config_file is not None and config_file not in part_configs:
        raise PipelineError("clip renders were made with another configuration file")
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
    summary = landscape.summarize_delivery(
        manifest, delivery, settings.clips, settings.height
    )
    summary["topic_content_review"] = "attested_in_selection_file"
    # Merge uses stored quality/media receipts; do not resolve fonts or invoke ffmpeg.
    summary["reproducibility"] = landscape.code_identity()
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
                RUN_FIELDS,
            )
        )
        if not successful_stage_run(run, request):
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


def write_receipt(
    output,
    result,
    lineage,
    request_id,
    source_ref,
    stage,
    workflow_run,
    clip_index=None,
    config_file=None,
):
    """Receipt the retained files identically for hosted stages and local merge."""
    hashes = {
        str(p.relative_to(output)): digest(p)
        for p in output.rglob("*")
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(output).parts)
        and p.name != "request-receipt.json"
    }
    receipt = {
        "request_id": request_id,
        "source_ref": source_ref,
        "workflow_run": workflow_run,
        "status": result["status"],
        "audio_source_url": result["source_url"],
        "sha256": hashes,
        "asr_reused": True,
        "audio_stage": stage,
        **lineage,
    }
    if clip_index is not None:
        receipt["clip_index"] = clip_index
    if config_file is not None:
        receipt["config_file"] = config_file
    write_json(output / "request-receipt.json", receipt)


def verify_same_run_analysis(previous, request_id, source_ref, run_id, previous_id):
    """Only the chained ClipForge previews job may consume an unfinished run's analysis."""
    if (
        os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_WORKFLOW") != "ClipForge"
        or os.environ.get("GITHUB_JOB") != "previews"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_RUN_ID") != run_id
        or os.environ.get("REQUEST_ID") != request_id
        or os.environ.get("CLIPFORGE_SOURCE_REF") != source_ref
        or previous_id != request_id
    ):
        raise PipelineError("same-run analysis execution identity mismatch")
    receipt = json.loads(private_member(previous, "request-receipt.json").read_text())
    if (
        receipt.get("request_id") != request_id
        or receipt.get("source_ref") != source_ref
        or str(receipt.get("workflow_run")) != run_id
        or receipt.get("status") != "topics_allocated"
    ):
        raise PipelineError("same-run analysis receipt identity mismatch")
    hashes = receipt.get("sha256")
    if not isinstance(hashes, dict) or not 1 <= len(hashes) <= 200:
        raise PipelineError("same-run analysis receipts are incomplete")
    for name, sha in hashes.items():
        if (
            not isinstance(sha, str)
            or not re.fullmatch(r"[a-f0-9]{64}", sha)
            or digest(private_member(previous, name)) != sha
        ):
            raise PipelineError("same-run analysis files changed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", choices=("previews", "render", "merge"), required=True
    )
    parser.add_argument("--previous-run")
    parser.add_argument("--previous-request")
    parser.add_argument("--same-run-analysis", type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/audio-stage"))
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--clip-index", type=clip_index_argument)
    parser.add_argument("--part-dir", type=Path, action="append", default=[])
    parser.add_argument("--part-runs", default="")
    parser.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG_FILE))
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
            or (
                args.same_run_analysis is not None
                and (args.stage != "previews" or args.review_file is not None)
            )
            # Stage requests name a committed configuration file. The chained previews
            # job instead passes the settings submitted with the run, written by the
            # workflow from its validated settings_json.
            or (
                args.same_run_analysis is None
                and (
                    args.config.parent != Path(".")
                    or not re.fullmatch(CONFIG_FILE_PATTERN, args.config.name)
                )
            )
        ):
            raise PipelineError("invalid private worker identity")
        if args.stage != "merge" and (
            not re.fullmatch(r"[0-9]{1,20}", args.previous_run or "")
            or not re.fullmatch(r"[a-f0-9]{32}", args.previous_request or "")
        ):
            raise PipelineError("invalid private worker identity")
        settings = Settings.model_validate_json(args.config.read_text())
        # 360p is the fast review and delivery size; 1080p is rendered on request.
        if (
            settings.aspect != "16:9"
            or settings.height not in (360, 1080)
            or settings.quality_gate != "strict"
        ):
            raise PipelineError("horizontal worker settings are required")
        if args.stage == "merge":
            with tempfile.TemporaryDirectory(prefix="clipforge-parts-") as temporary:
                parts = [(path, None) for path in args.part_dir]
                parts += download_parts(args.part_runs, Path(temporary))
                result, lineage = merge_parts(
                    parts,
                    args.output,
                    settings,
                    args.source_ref,
                    config_file=args.config.name,
                )
        elif args.same_run_analysis is not None:
            verify_same_run_analysis(
                args.same_run_analysis,
                args.request_id,
                args.source_ref,
                args.previous_run,
                args.previous_request,
            )
            result = execute(
                args.same_run_analysis,
                args.output,
                args.previous_request,
                settings,
                "previews",
            )
            # Internal stage caches are not deliverables and are not receipted.
            shutil.rmtree(args.output / ".state", ignore_errors=True)
            lineage = {
                "previous_run_id": args.previous_run,
                "previous_request_id": args.previous_request,
            }
        else:
            run = json.loads(
                gh(
                    "run",
                    "view",
                    args.previous_run,
                    "--repo",
                    REPO,
                    "--json",
                    RUN_FIELDS,
                )
            )
            if not successful_stage_run(run, args.previous_request):
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
        write_receipt(
            args.output,
            result,
            lineage,
            args.request_id,
            args.source_ref,
            args.stage,
            os.environ.get("GITHUB_RUN_ID"),
            args.clip_index,
            config_file=None
            if args.same_run_analysis is not None
            else args.config.name,
        )
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
            **failure_fields(error),
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
