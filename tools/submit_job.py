#!/usr/bin/env python3
"""VM gateway: preflight URL audio, stage private input, dispatch, retrieve."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from clipforge import github_cli
from clipforge.storage import digest, write_json

REPO = "nyu1791-collab/ai-model-workers"
SOURCE = "nyu1791-collab/ClipForge"
WORKFLOW = "clipforge.yml"
# Each Colab job owns its own T4. The account was measured to grant two T4 runtimes at once
# (a third was refused on 2026-10-22), so two jobs may run side by side; the hosted CPU
# runner keeps one job at a time.
MAX_ACTIVE = {"colab-auto": 2}
MAX_SETTINGS_JSON_BYTES = 16 * 1024
SETTINGS_FIELDS = {
    "gemini_model",
    "clips",
    "candidates",
    "min_seconds",
    "max_seconds",
    "whisper_model",
    "whisper_revision",
    "asr_batch_size",
    "language",
    "threads",
    "encode_threads",
    "transcript_key",
    "whisper_device",
    "whisper_compute_type",
    "video_encoder",
    "aspect",
    "framing",
    "height",
    "encode_preset",
    "caption",
    "caption_scale",
    "font",
    "horizontal_target_cps",
    "horizontal_warn_cps",
    "horizontal_line_chars",
    "layout",
    "heading",
    "face_box",
    "game_box",
    "zoom",
    "workers",
    "loudness_two_pass",
    "silence_cut",
    "silence_pad",
    "silence_min_gap",
    "vad",
    "topics",
    "topic_min_seconds",
    "topic_max_seconds",
    "diagrams",
    "image_search",
    "image_sources",
    "table_query",
    "bgm_gain_db",
    "ducking",
    "thumbnail",
    "fade_seconds",
    "progress_bar",
    "quality_gate",
    "analysis_fps",
    "max_source_seconds",
    "max_input_bytes",
    "max_transport_bytes",
    "chat_url",
    "chat_offset_seconds",
    "chat_panel",
    "chat_panel_width",
    "chat_panel_fit",
    "chat_anonymize",
}
# These name files on the submitting machine; the remote worker cannot read them.
LOCAL_FILE_FIELDS = {
    "visuals",
    "bgm",
    "sound_effects",
    "transcript_cache",
    "require_transcript_cache",
}


def validate_settings(settings):
    if (
        not isinstance(settings, dict)
        or set(settings) - SETTINGS_FIELDS - LOCAL_FILE_FIELDS
    ):
        raise RuntimeError(
            "configuration has unknown fields; put API credentials in Secrets, not this file"
        )
    if set(settings) & LOCAL_FILE_FIELDS:
        raise RuntimeError(
            "visuals, bgm, sound_effects and transcript_cache name local files; render those clips locally"
        )


def parse_settings_json(value):
    """Validate dispatch settings using the same allowlist as local submissions."""
    if len(value.encode("utf-8")) > MAX_SETTINGS_JSON_BYTES:
        raise RuntimeError("settings_json exceeds the 16 KB limit")
    settings = json.loads(value)
    validate_settings(settings)
    return settings


def preflight_audio_url(url, settings):
    """Read metadata and the lowest playlist only, before a runner is dispatched."""
    from clipforge.kick_media import KickVOD
    from clipforge.models import Settings
    from clipforge.storage import PipelineError
    from pydantic import ValidationError

    try:
        settings = Settings.model_validate(settings)
    except ValidationError as error:
        reasons = "; ".join(
            ".".join(map(str, row["loc"])) + ": " + row["msg"]
            for row in error.errors(include_input=False)
        )
        raise RuntimeError("invalid audio URL settings: " + reasons) from None
    with tempfile.TemporaryDirectory(prefix="clipforge-preflight-") as temporary:
        try:
            # Read beyond the job's duration limit so rejection includes the actual length.
            vod = KickVOD(
                url,
                Path(temporary) / "segments",
                max_bytes=settings.max_transport_bytes,
                max_seconds=86400,
            )
            try:
                segments, _ = vod.playlist("lowest")
            finally:
                vod.close()
        except PipelineError as error:
            raise RuntimeError("audio URL preflight failed: " + str(error)) from None
    duration = segments[-1]["end"]
    if duration > settings.max_source_seconds:
        raise RuntimeError(
            f"archive duration {duration:g} seconds exceeds configured limit "
            f"{settings.max_source_seconds:g} seconds; no workflow dispatched"
        )


def gh(*args, body=None, timeout=120):
    # The shared runner never prints stderr, where gh may echo signed URLs.
    return github_cli.run(
        *args,
        body=body,
        timeout=timeout,
        include_command=True,
        include_http_status=True,
    )


def save(path, receipt):
    write_json(path, receipt)


def plain_tree(directory):
    """List regular files and directories; refuse symlinks and special entries."""
    files, dirs = set(), set()
    for current, dirnames, filenames in os.walk(directory):
        for name in dirnames + filenames:
            if Path(current, name).is_symlink():
                raise RuntimeError("result contains a symlink")
        for name in dirnames:
            dirs.add(Path(current, name).relative_to(directory).as_posix())
        for name in filenames:
            path = Path(current, name)
            if not path.is_file():
                raise RuntimeError("result contains a non-regular entry")
            files.add(path.relative_to(directory).as_posix())
    return files, dirs


def verify_audio_analysis(
    directory, identity, source_ref, source_sha256, source_url=None, run_id=None
):
    """Verify a topics_allocated artifact. Without a local record, source_url is the artifact's own canonical VOD URL."""
    from clipforge.kick_media import canonical
    from clipforge.models import Transcript
    from clipforge.storage import TIME_TOLERANCE

    files, dirs = plain_tree(directory)
    if not {"transcripts"} <= dirs <= {"transcripts", ".state"}:
        raise RuntimeError("result contains unexpected directories")
    returned = json.loads((directory / "request-receipt.json").read_text())
    if source_url is None:
        source_url = canonical(str(returned.get("audio_source_url")))
    if (
        returned.get("request_id") != identity
        or returned.get("source_ref") != source_ref
        or returned.get("status") != "topics_allocated"
        or returned.get("audio_source_url") != source_url
        or (run_id is not None and str(returned.get("workflow_run")) != str(run_id))
    ):
        raise RuntimeError("result identity or completion state mismatch")
    cache = json.loads((directory / "asr-receipt.json").read_text())
    key = cache.get("key", "") if isinstance(cache, dict) else ""
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise RuntimeError("audio analysis cache identity missing")
    names = {
        "audio.m4a",
        "manifest.json",
        "topics.json",
        "asr-receipt.json",
        "transcripts/" + key + ".json",
    }
    # Internal bookkeeping and top-level diagnostic files are not hashed.
    internal = {
        ".audio-first.lock",
        ".state/topics.json",
        "gpu-receipt.json",
        "acquisition.json",
        "audio.m4a.receipt.json",
    }
    required = names | {"request-receipt.json"}
    if not required <= files or files - required - internal:
        raise RuntimeError("result contains unexpected files")
    hashes = returned.get("sha256", {})
    if (
        not isinstance(hashes, dict)
        or set(hashes) != names
        or any(digest(directory / name) != hashes[name] for name in names)
    ):
        raise RuntimeError("audio analysis output checksum mismatch")
    if source_sha256 is None:
        source_sha256 = hashes["audio.m4a"]
    manifest = json.loads((directory / "manifest.json").read_text())
    if (
        manifest.get("status") != "topics_allocated"
        or manifest.get("source_url") != source_url
        or manifest.get("source_sha256") != source_sha256
        or manifest.get("rendered")
        or manifest.get("model_generation_requests") != 0
        or digest(directory / "audio.m4a") != source_sha256
    ):
        raise RuntimeError("audio analysis identity mismatch")
    Transcript.model_validate_json(
        (directory / "transcripts" / (key + ".json")).read_text()
    )
    topics = json.loads((directory / "topics.json").read_text())
    rows = topics.get("topics") if isinstance(topics, dict) else None
    if (
        not isinstance(topics, dict)
        or topics.get("status") != "topics_allocated"
        or topics.get("source_url") != source_url
        or topics.get("model_generation_requests") != 0
        or not isinstance(rows, list)
    ):
        raise RuntimeError("topics file identity or format mismatch")
    duration = topics.get("duration")
    seen = set()
    for row in rows:
        numbers = (
            [row.get(field) for field in ("start", "end")]
            if isinstance(row, dict)
            else []
        )
        if (
            not isinstance(row, dict)
            or not re.fullmatch(r"topic-[0-9]{3}", str(row.get("id")))
            or row["id"] in seen
            or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in numbers
            )
            or not 0 <= row["start"] < row["end"]
            or (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and row["end"] > duration + TIME_TOLERANCE
            )
        ):
            raise RuntimeError("topics file row format mismatch")
        seen.add(row["id"])


def verify_download(directory, receipt, request):
    if receipt.get("target_stage") == "previews":
        from submit_audio_stage import verify_stage_artifact

        returned = json.loads((directory / "request-receipt.json").read_text())
        if (
            returned.get("audio_source_url") != request["audio_source_url"]
            or str(returned.get("previous_run_id")) != str(receipt["run_id"])
            or returned.get("previous_request_id") != receipt["request_id"]
        ):
            raise RuntimeError("chained previews lineage or source mismatch")
        verify_stage_artifact(
            directory,
            receipt["request_id"],
            "previews",
            {
                **request,
                "previous_run_id": str(receipt["run_id"]),
                "previous_request_id": receipt["request_id"],
            },
            receipt["run_id"],
        )
        for name in ("review-draft.json", "review-draft-diagnostics.json"):
            if name not in returned["sha256"]:
                raise RuntimeError("chained previews draft is missing")
        return
    if request.get("audio_source_url"):
        # URL-only submissions have no local audio hash. The returned audio is the
        # runner-acquired input, checked against both its manifest and file receipt.
        verify_audio_analysis(
            directory,
            receipt["request_id"],
            receipt["source_ref"],
            request["video_sha256"],
            request["audio_source_url"],
        )
        return
    plain_tree(directory)
    returned = json.loads((directory / "request-receipt.json").read_text())
    if (
        returned.get("request_id") != receipt["request_id"]
        or returned.get("source_ref") != receipt["source_ref"]
        or returned.get("status") != "complete"
    ):
        raise RuntimeError("result identity or completion state mismatch")
    manifest = json.loads((directory / "manifest.json").read_text())
    if (
        manifest.get("status") != "complete"
        or manifest.get("source_sha256") != request["video_sha256"]
    ):
        raise RuntimeError("result manifest does not match the input")
    rendered = manifest.get("rendered", [])
    if not 1 <= len(rendered) <= request["settings"].get("clips", 3) or len(
        rendered
    ) != returned.get("rendered_clips"):
        raise RuntimeError("result clip count mismatch")
    seen = set()
    for clip in rendered:
        relative = clip["directory"]
        if not re.fullmatch(r"clips/clip-[0-9]{3}", relative) or relative in seen:
            raise RuntimeError("unexpected output directory")
        seen.add(relative)
        checksums = clip.get("sha256", {})
        if set(checksums) != {"video.mp4", "captions.ass", "captions.srt"}:
            raise RuntimeError("missing output checksums")
        for name, checksum in checksums.items():
            target = directory / relative / name
            if (
                any(part.is_symlink() for part in [target, *target.parents])
                or digest(target) != checksum
            ):
                raise RuntimeError("downloaded output checksum mismatch")


def prepare_destination(destination):
    destination = Path(destination).absolute()
    if destination.exists() or any(
        part.is_symlink() for part in [destination, *destination.parents]
    ):
        raise RuntimeError(
            "download destination already exists or uses a symlink; choose a new directory"
        )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return destination


def download_verified(run_id, identity, destination, verify, artifact_stage="result"):
    """Download into a private staging directory, verify it, then move it into place."""
    with tempfile.TemporaryDirectory(
        prefix=".clipforge-download-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "result"
        gh(
            "run",
            "download",
            str(run_id),
            "--repo",
            REPO,
            "--name",
            "clipforge-" + artifact_stage + "-" + identity,
            "--dir",
            str(staging),
            timeout=300,
        )
        verify(staging)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("download destination was created by another operation")
        staging.rename(destination)


def collect_result(state, receipt, destination, collect_stage=None):
    destination = prepare_destination(destination)
    request = json.loads((state / "request.json").read_text())
    analysis = collect_stage == "analysis" and receipt.get("target_stage") == "previews"

    def verify(staging):
        if analysis:
            verify_audio_analysis(
                staging,
                receipt["request_id"],
                receipt["source_ref"],
                request["video_sha256"],
                request["audio_source_url"],
                receipt["run_id"],
            )
        else:
            verify_download(staging, receipt, request)

    try:
        download_verified(
            receipt["run_id"],
            receipt["request_id"],
            destination,
            verify,
            artifact_stage="analysis" if analysis else "result",
        )
    except Exception:
        receipt["analysis_collection_status" if analysis else "status"] = (
            "DOWNLOAD_FAILED"
        )
        save(state / "receipt.json", receipt)
        raise
    if analysis:
        receipt.update(
            analysis_collection_status="COMPLETE",
            analysis_output=str(destination.resolve()),
        )
    else:
        receipt.update(status="COMPLETE", output=str(destination.resolve()))
        receipt["input_cleanup"] = input_cleanup_status(receipt["input_tag"])
    save(state / "receipt.json", receipt)
    print("Completed; private outputs verified and downloaded.")
    if receipt.get("input_cleanup") == "REMAINS":
        print(
            "Warning: the temporary private input release still exists. Delete it with "
            "tools/submit_job.py --cleanup-tag " + receipt["input_tag"],
            file=sys.stderr,
        )


def input_cleanup_status(tag):
    """Whether the worker removed the temporary private input; never fails a collection."""
    if not re.fullmatch(r"clipforge-input-[a-f0-9]{32}", str(tag)):
        return "UNKNOWN"
    try:
        gh("api", f"repos/{REPO}/releases/tags/{tag}", "--jq", ".tag_name")
    except RuntimeError as error:
        return "DELETED" if "HTTP 404" in str(error) else "UNKNOWN"
    except Exception:
        return "UNKNOWN"
    return "REMAINS"


def local_record_exists(identity):
    return (
        bool(re.fullmatch(r"[a-f0-9]{32}", identity))
        and (
            Path.home() / ".local/state/clipforge" / identity / "receipt.json"
        ).is_file()
    )


def collect_without_record(args):
    """Collect a topics_allocated audio analysis on a machine with no local state record."""
    identity = args.collect
    if args.collect_stage is not None:
        raise RuntimeError("--collect-stage requires the saved local request record")
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise RuntimeError("invalid request identifier")
    if (
        args.run_id is None
        or args.source_ref is None
        or args.audio_sha256 is None
        or args.output is None
    ):
        raise RuntimeError(
            "no local record: --run-id, --source-ref, --audio-sha256 and --output are required"
        )
    if (
        args.run_id < 1
        or not re.fullmatch(r"[a-f0-9]{40}", args.source_ref)
        or not re.fullmatch(r"[a-f0-9]{64}", args.audio_sha256)
    ):
        raise RuntimeError("invalid run ID, source commit, or audio SHA-256")
    run = json.loads(gh("api", f"repos/{REPO}/actions/runs/{args.run_id}"))
    if (
        run.get("id") != args.run_id
        or run.get("display_title") != "ClipForge " + identity
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise RuntimeError("workflow run identity or completion state mismatch")
    destination = prepare_destination(args.output)
    download_verified(
        args.run_id,
        identity,
        destination,
        lambda staging: verify_audio_analysis(
            staging, identity, args.source_ref, args.audio_sha256
        ),
    )
    print("Completed; private outputs verified and downloaded.")


def find_run(identity):
    runs = json.loads(
        gh(
            "run",
            "list",
            "--repo",
            REPO,
            "--workflow",
            WORKFLOW,
            "--limit",
            "100",
            "--json",
            "databaseId,displayTitle,event",
        )
    )
    return next(
        (
            r["databaseId"]
            for r in runs
            if r["displayTitle"] == "ClipForge " + identity
            and r["event"] == "workflow_dispatch"
        ),
        None,
    )


def send_dispatch(state, receipt):
    """Record the uncertain interval before POST, so a lost reply cannot cause a retry."""
    settings_json = "{}"
    if receipt.get("audio_url"):
        request = json.loads((state / "request.json").read_text())
        settings_json = json.dumps(request["settings"], ensure_ascii=False)
        parse_settings_json(settings_json)
    receipt["status"] = "DISPATCHING"
    receipt["job_submitted"] = None
    receipt.pop("diagnostic_code", None)
    save(state / "receipt.json", receipt)
    payload = json.dumps(
        {
            "ref": "main",
            "inputs": {
                "request_id": receipt["request_id"],
                "input_tag": receipt["input_tag"],
                "source_ref": receipt["source_ref"],
                # Receipts written before worker selection ran on the hosted CPU runner.
                "worker": receipt.get("worker", "cpu"),
                "audio_url": receipt.get("audio_url", ""),
                "settings_json": settings_json,
                "public_cache": receipt.get("public_cache", "off"),
                **({"auto_previews": True} if receipt.get("auto_previews") else {}),
            },
        }
    )
    try:
        gh(
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
            "--input",
            "-",
            body=payload,
        )
    except Exception as error:
        rejected = re.search(r"HTTP (400|401|403|404|422)\b", str(error))
        receipt["status"] = "DISPATCH_REJECTED" if rejected else "DISPATCH_UNCONFIRMED"
        save(state / "receipt.json", receipt)
        raise
    receipt["status"] = "DISPATCHED"
    receipt["job_submitted"] = True
    save(state / "receipt.json", receipt)


def resume_audio_staging(args):
    """Resume one saved audio request; never create another request ID or redo ASR."""
    identity = args.resume_audio
    if not re.fullmatch(r"[a-f0-9]{32}", identity) or args.input is None:
        raise RuntimeError(
            "resume requires a valid request ID and the original --input audio"
        )
    state_root = Path.home() / ".local/state/clipforge"
    with (state_root / ".submit.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another VM submission is active") from None
        state = state_root / identity
        receipt = json.loads((state / "receipt.json").read_text())
        request_path = state / "request.json"
        request = json.loads(request_path.read_text())
        if (
            receipt.get("repository") != REPO
            or receipt.get("request_id") != identity
            or request.get("request_id") != identity
            or receipt.get("input_tag") != "clipforge-input-" + identity
            or receipt.get("source_ref") != request.get("source_ref")
            or not re.fullmatch(r"[a-f0-9]{40}", str(receipt.get("source_ref")))
            or not request.get("audio_source_url")
            or request.get("chat_sha256")
            or request.get("settings", {}).get("gemini_model") != "manual-no-api"
        ):
            raise RuntimeError("saved audio request identity mismatch")
        run_id = receipt.get("run_id") or find_run(identity)
        if run_id:
            receipt.update(
                run_id=run_id, url=f"https://github.com/{REPO}/actions/runs/{run_id}"
            )
            save(state / "receipt.json", receipt)
            print("Existing request already has a workflow; no new job was submitted.")
            return
        if receipt.get("status") not in {
            "STAGING_FAILED",
            "UPLOAD_FAILED_AUTH",
            "STAGING_PARTIAL",
            "DISPATCH_REJECTED",
        }:
            raise RuntimeError(
                "dispatch state is uncertain or not resumable; inspect the existing request"
            )
        source = args.input.resolve(strict=True)
        if (
            digest(source) != request["video_sha256"]
            or source.stat().st_size != request["video_bytes"]
        ):
            raise RuntimeError("original audio checksum or size mismatch")
        active = json.loads(
            gh("run", "list", "--repo", REPO, "--limit", "100", "--json", "status")
        )
        if any(row["status"] != "completed" for row in active):
            raise RuntimeError("another video job is active; wait before resuming")
        endpoint = f"repos/{REPO}/releases/tags/{receipt['input_tag']}"
        try:
            release = json.loads(gh("api", endpoint))
        except RuntimeError as error:
            if "HTTP 404" not in str(error):
                raise
            release = json.loads(
                gh(
                    "api",
                    "--method",
                    "POST",
                    f"repos/{REPO}/releases",
                    "--input",
                    "-",
                    body=json.dumps(
                        {
                            "tag_name": receipt["input_tag"],
                            "target_commitish": "main",
                            "name": receipt["input_tag"],
                            "make_latest": "false",
                        }
                    ),
                )
            )

        def verify_assets(value):
            expected = {"input-video.bin": source, "request.json": request_path}
            present = {}
            for row in value["assets"]:
                name = row["name"]
                if name not in expected or name in present:
                    raise RuntimeError("unexpected or duplicate staged asset")
                path = expected[name]
                if (
                    row.get("digest") != "sha256:" + digest(path)
                    or row["size"] != path.stat().st_size
                ):
                    raise RuntimeError("existing staged asset checksum mismatch")
                present[name] = path
            return {
                name: path for name, path in expected.items() if name not in present
            }

        missing = verify_assets(release)
        if missing:
            with tempfile.TemporaryDirectory(prefix="clipforge-resume-") as temporary:
                files = []
                for name, source_path in missing.items():
                    target = Path(temporary) / name
                    target.symlink_to(source_path)
                    files.append(str(target))
                try:
                    gh(
                        "release",
                        "upload",
                        receipt["input_tag"],
                        *files,
                        "--repo",
                        REPO,
                        timeout=1800,
                    )
                except Exception as error:
                    # Only a credential rejection is labelled as auth; other failures
                    # stay resumable, and the next run re-verifies every asset checksum.
                    rejected = isinstance(error, RuntimeError) and re.search(
                        r"HTTP (401|403)\b", str(error)
                    )
                    receipt["status"] = (
                        "UPLOAD_FAILED_AUTH" if rejected else "STAGING_PARTIAL"
                    )
                    save(state / "receipt.json", receipt)
                    raise
            if verify_assets(json.loads(gh("api", endpoint))):
                raise RuntimeError("staged input upload is incomplete")
        # No dispatch is attempted until every staged file matches its saved request.
        send_dispatch(state, receipt)
    print("Resumed private audio request " + identity + "; one workflow submitted.")


def record_run_status(state, receipt, run):
    """Keep successful analysis distinct from a failed chained preview job."""
    receipt.update(github_status=run["status"], conclusion=run.get("conclusion"))
    if receipt.get("target_stage") == "previews":
        jobs = {job["name"]: job for job in run.get("jobs", [])}
        for job_name, field in (
            ("colab-auto", "analysis_status"),
            ("previews", "previews_status"),
        ):
            job = jobs.get(job_name, {})
            receipt[field] = job.get("conclusion") or job.get("status") or "unknown"
    if run["status"] == "completed" and run.get("conclusion") != "success":
        receipt["status"] = (
            "PARTIAL_FAILED"
            if receipt.get("analysis_status") == "success"
            else "FAILED"
        )
    save(state / "receipt.json", receipt)


def recover(args):
    identity = args.collect or args.status
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise RuntimeError("invalid request identifier")
    state = Path.home() / ".local/state/clipforge" / identity
    receipt = json.loads((state / "receipt.json").read_text())
    if receipt.get("repository") != REPO or receipt.get("request_id") != identity:
        raise RuntimeError("saved request identity mismatch")
    run_id = receipt.get("run_id") or find_run(identity)
    if not run_id:
        if args.status:
            print(json.dumps({**receipt, "workflow_found": False}, indent=2))
            return
        raise RuntimeError(
            "no matching workflow run found; inspect the staged input before resubmitting"
        )
    run = json.loads(
        gh(
            "run",
            "view",
            str(run_id),
            "--repo",
            REPO,
            "--json",
            "status,conclusion,jobs"
            if receipt.get("target_stage") == "previews"
            else "status,conclusion",
        )
    )
    receipt.update(
        run_id=run_id,
        job_submitted=True,
        github_status=run["status"],
        conclusion=run.get("conclusion"),
        url=f"https://github.com/{REPO}/actions/runs/{run_id}",
    )
    record_run_status(state, receipt, run)
    if args.status:
        print(json.dumps(receipt, indent=2))
        return
    analysis = (
        args.collect_stage == "analysis" and receipt.get("target_stage") == "previews"
    )
    if args.collect_stage == "previews" and receipt.get("target_stage") != "previews":
        raise RuntimeError("this request did not target previews")
    if run["status"] != "completed" or (
        receipt.get("analysis_status") != "success"
        if analysis
        else run.get("conclusion") != "success"
    ):
        raise RuntimeError(
            "request has not completed successfully; no new generation was submitted"
        )
    collect_result(
        state,
        receipt,
        args.output or state / ("analysis" if analysis else "result"),
        args.collect_stage,
    )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--gemini-model")
    parser.add_argument(
        "--audio-analysis-url",
        help="allocate topics from a Kick VOD or local audio; no Gemini calls",
    )
    parser.add_argument("--ref", default="main")
    parser.add_argument("--worker", choices=["colab-auto", "cpu"], default="colab-auto")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--chat-file", type=Path)
    parser.add_argument("--clips", type=int)
    parser.add_argument("--chat-url")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--auto-previews", action="store_true")
    parser.add_argument(
        "--public-cache",
        choices=("on", "off"),
        default="off",
        help="restore and publish the GPU public cache on Colab (default: off; fresh setup was faster, 40 s vs 104 s)",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=18000,
        help="maximum wait in seconds (default: 18000)",
    )
    parser.add_argument("--collect-stage", choices=("analysis", "previews"))
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--run-id",
        type=int,
        help="with --collect and no local record: the workflow run to verify",
    )
    parser.add_argument(
        "--source-ref",
        help="with --collect and no local record: the reviewed full commit SHA",
    )
    parser.add_argument(
        "--audio-sha256",
        help="with --collect and no local record: the expected audio SHA-256",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--collect",
        help="Recover and verify one existing request without generating again",
    )
    modes.add_argument(
        "--status", help="Show one existing request and GitHub run status"
    )
    modes.add_argument(
        "--resume-audio", help="Resume a failed audio upload using its saved request ID"
    )
    modes.add_argument(
        "--cleanup-tag",
        help="Delete one temporary private input release after inspecting its job",
    )
    args = parser.parse_args()
    if args.wait_timeout <= 0:
        parser.error("--wait-timeout must be positive")
    if args.collect_stage and not args.collect:
        parser.error("--collect-stage requires --collect")
    if args.auto_previews and (
        not args.audio_analysis_url
        or args.input is not None
        or args.worker != "colab-auto"
        or args.collect
        or args.status
        or args.resume_audio
        or args.cleanup_tag
    ):
        parser.error(
            "--auto-previews requires URL-only submission with --worker colab-auto"
        )
    if gh("api", "user", "--jq", ".login").strip() != "nyu1791-collab":
        raise RuntimeError("use the authenticated owner VM connection")
    if not gh("api", "repos/" + REPO, "--jq", ".private").strip() == "true":
        raise RuntimeError("worker repository must remain private")
    if args.cleanup_tag:
        if not re.fullmatch(r"clipforge-input-[a-f0-9]{32}", args.cleanup_tag):
            raise RuntimeError("invalid temporary input tag")
        gh(
            "release",
            "delete",
            args.cleanup_tag,
            "--repo",
            REPO,
            "--yes",
            "--cleanup-tag",
        )
        print("Temporary private input deleted.")
        return
    if args.collect and not local_record_exists(args.collect):
        collect_without_record(args)
        return
    if args.collect or args.status:
        recover(args)
        return
    if args.resume_audio:
        resume_audio_staging(args)
        return
    if args.audio_analysis_url:
        from clipforge.kick_media import canonical

        canonical(args.audio_analysis_url)
        args.gemini_model = "manual-no-api"
    if args.audio_analysis_url and args.input is None and args.worker != "colab-auto":
        parser.error("URL-only audio analysis requires --worker colab-auto")
    if (args.input is None and not args.audio_analysis_url) or not args.gemini_model:
        parser.error("--input and --gemini-model are required to submit a video")
    if args.output:
        args.wait = True
        if args.output.exists() or args.output.is_symlink():
            raise RuntimeError(
                "download destination already exists; choose a new directory"
            )
    submit(args)


def submit(args):
    state_root = Path.home() / ".local/state/clipforge"
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_root / ".submit.lock").open("a") as submit_lock:
        try:
            fcntl.flock(submit_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another VM submission is active") from None
        state, receipt = dispatch(args, state_root)
    wait_for_result(args, state, receipt)


def dispatch(args, state_root):
    workflow = json.loads(gh("api", f"repos/{REPO}/actions/workflows/{WORKFLOW}"))
    if workflow.get("state") != "active":
        raise RuntimeError("private ClipForge workflow is not active")
    runs = json.loads(
        gh(
            "run",
            "list",
            "--repo",
            REPO,
            "--workflow",
            WORKFLOW,
            "--limit",
            "100",
            "--json",
            "status",
        )
    )
    active = sum(run["status"] != "completed" for run in runs)
    if active >= MAX_ACTIVE.get(args.worker, 1):
        raise RuntimeError(
            "another video job is active; wait before submitting a new input"
        )
    # Audio analysis on the hosted CPU runner needs neither the Colab nor the Gemini secret.
    needs_secrets = args.worker == "colab-auto" or not args.audio_analysis_url
    names = None
    if needs_secrets:
        try:
            names = [
                secret["name"]
                for secret in json.loads(
                    gh("secret", "list", "--repo", REPO, "--json", "name")
                )
            ]
        except RuntimeError as error:
            # A token without secrets:read gets 403. The worker checks COLAB_ADC_JSON before
            # any GPU is allocated, so the request may go ahead and fail there instead.
            if "(HTTP 403)" not in str(error):
                raise
            print(
                "Secret names are not readable with this token; the worker checks them.",
                flush=True,
            )
    if names is not None:
        if args.worker == "colab-auto" and "COLAB_ADC_JSON" not in names:
            raise RuntimeError(
                "COLAB_ADC_JSON is not registered; complete the intended Google account's one-time OAuth setup"
            )
        if not args.audio_analysis_url and "GEMINI_API_KEY" not in names:
            raise RuntimeError("private worker GEMINI_API_KEY is not registered")
    source = args.input.resolve(strict=True) if args.input else None
    overrides = json.loads(args.config.read_text()) if args.config else {}
    validate_settings(overrides)
    url_only = bool(args.audio_analysis_url and source is None)
    settings = {}
    if url_only:
        settings = json.loads(
            (
                Path(__file__).resolve().parents[1] / "config.landscape.example.json"
            ).read_text()
        )
        validate_settings(settings)
    settings.update(overrides)
    settings["gemini_model"] = args.gemini_model
    if args.audio_analysis_url and (
        settings.get("image_search")
        or settings.get("table_query")
        or settings.get("chat_url")
        or args.chat_url
        or args.chat_file
    ):
        raise RuntimeError("audio analysis cannot include external lookups or chat")
    if args.clips is not None:
        settings["clips"] = args.clips
    clips = settings.get("clips", 3)
    if args.chat_url:
        settings["chat_url"] = args.chat_url
    if not re.fullmatch(r"(?:models/)?[A-Za-z0-9._-]+", args.gemini_model):
        raise RuntimeError("invalid Gemini model identifier")
    if type(clips) is not int or not 1 <= clips <= 10:
        raise RuntimeError("clip count must be between 1 and 10")
    if url_only:
        parse_settings_json(json.dumps(settings, ensure_ascii=False))
        preflight_audio_url(args.audio_analysis_url, settings)
    maximum = settings.get("max_input_bytes", 1073741824)
    if source is not None and (
        not source.is_file()
        or type(maximum) is not int
        or not 0 < source.stat().st_size <= min(maximum, 2147483648)
    ):
        raise RuntimeError("video must be a regular file within the input size limit")
    if args.chat_file and (
        not args.chat_file.is_file()
        or not 0 < args.chat_file.stat().st_size <= 20000000
    ):
        raise RuntimeError("chat JSON must be a file no larger than 20MB")
    sha = json.loads(gh("api", f"repos/{SOURCE}/commits/{args.ref}"))["sha"]
    identity = uuid.uuid4().hex
    tag = "clipforge-input-" + identity
    state = state_root / identity
    state.mkdir(parents=True, mode=0o700)
    receipt = {
        "request_id": identity,
        "repository": REPO,
        "source_ref": sha,
        "input_tag": tag,
        "status": "STAGING",
        "worker": args.worker,
        "audio_url": args.audio_analysis_url if source is None else "",
        "public_cache": args.public_cache,
        "input_staged": False,
    }
    if args.auto_previews:
        receipt.update(auto_previews=True, target_stage="previews")
    receipt_path = state / "receipt.json"
    save(receipt_path, receipt)
    print("Private job receipt: " + str(receipt_path), flush=True)
    request = {
        "request_id": identity,
        "source_ref": sha,
        "video_sha256": digest(source) if source is not None else None,
        "video_bytes": source.stat().st_size if source is not None else None,
        "settings": settings,
        "chat_sha256": digest(args.chat_file) if args.chat_file else None,
    }
    if args.audio_analysis_url:
        request["audio_source_url"] = args.audio_analysis_url
    save(state / "request.json", request)
    if source is not None:
        with tempfile.TemporaryDirectory(prefix="clipforge-submit-") as temporary:
            directory = Path(temporary)
            video = directory / "input-video.bin"
            video.symlink_to(source)
            manifest = directory / "request.json"
            manifest.write_bytes((state / "request.json").read_bytes())
            files = [str(video), str(manifest)]
            if args.chat_file:
                chat = directory / "chat.json"
                chat.symlink_to(args.chat_file.resolve())
                files.append(str(chat))
            try:
                gh(
                    "release",
                    "create",
                    tag,
                    *files,
                    "--repo",
                    REPO,
                    "--target",
                    "main",
                    "--title",
                    tag,
                    "--latest=false",
                    "--notes",
                    "Temporary private ClipForge job input. Automatically cleaned after execution.",
                    timeout=1800,
                )
            except Exception:
                receipt["status"] = "STAGING_FAILED"
                save(receipt_path, receipt)
                raise
        receipt.update(status="STAGED", input_staged=True)
        save(receipt_path, receipt)
    send_dispatch(state, receipt)
    return state, receipt


def wait_for_result(args, state, receipt):
    identity = receipt["request_id"]
    receipt_path = state / "receipt.json"
    print("Submitted private ClipForge job " + identity, flush=True)
    deadline = time.monotonic() + args.wait_timeout
    run_id = None
    while time.monotonic() < deadline:
        if run_id is None:
            runs = json.loads(
                gh(
                    "run",
                    "list",
                    "--repo",
                    REPO,
                    "--workflow",
                    WORKFLOW,
                    "--limit",
                    "100",
                    "--json",
                    "databaseId,displayTitle,event",
                )
            )
            matches = [
                r
                for r in runs
                if r["displayTitle"] == "ClipForge " + identity
                and r["event"] == "workflow_dispatch"
            ]
            if matches:
                run_id = matches[0]["databaseId"]
                receipt.update(
                    run_id=run_id,
                    url=f"https://github.com/{REPO}/actions/runs/{run_id}",
                    status="RUNNING",
                )
                save(receipt_path, receipt)
                print(receipt["url"], flush=True)
                if not args.wait:
                    return
        else:
            run = json.loads(
                gh(
                    "run",
                    "view",
                    str(run_id),
                    "--repo",
                    REPO,
                    "--json",
                    "status,conclusion,jobs"
                    if receipt.get("target_stage") == "previews"
                    else "status,conclusion",
                )
            )
            if run["status"] == "completed":
                record_run_status(state, receipt, run)
                if run["conclusion"] != "success":
                    raise RuntimeError(
                        "private job did not complete successfully; no retry or fallback"
                    )
                collect_result(state, receipt, args.output or state / "result")
                return
        time.sleep(15)
    receipt["status"] = "UNCONFIRMED"
    save(receipt_path, receipt)
    raise RuntimeError(
        "waiting timed out; inspect the recorded run before submitting another request"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        raise SystemExit(1) from None
