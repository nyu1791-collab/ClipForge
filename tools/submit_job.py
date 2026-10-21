#!/usr/bin/env python3
"""Standard-library-only VM gateway: stage private input, dispatch, retrieve."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

REPO = "nyu1791-collab/ai-model-workers"
SOURCE = "nyu1791-collab/ClipForge"
WORKFLOW = "clipforge.yml"


def gh(*args, body=None, timeout=120):
    result = subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        # Never print signed URLs that gh may include in raw stderr.
        raise RuntimeError("GitHub operation failed: " + args[0])
    return result.stdout


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            value.update(data)
    return value.hexdigest()


def save(path, receipt):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--gemini-model")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--chat-file", type=Path)
    parser.add_argument("--clips", type=int)
    parser.add_argument("--chat-url")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--cleanup-tag",
        help="Delete one temporary private input release after inspecting its job",
    )
    args = parser.parse_args()
    if gh("api", "user", "--jq", ".login").strip() != "nyu1791-collab":
        raise RuntimeError("use the authenticated owner VM connection")
    if not json.loads(gh("repo", "view", REPO, "--json", "isPrivate"))["isPrivate"]:
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
    if not args.input or not args.gemini_model:
        parser.error("--input and --gemini-model are required to submit a video")
    state_root = Path.home() / ".local/state/clipforge"
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    submit_lock = (state_root / ".submit.lock").open("a")
    try:
        fcntl.flock(submit_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another VM submission is active") from None
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
    if any(run["status"] != "completed" for run in runs):
        raise RuntimeError(
            "another video job is active; wait before submitting a new input"
        )
    names = json.loads(gh("secret", "list", "--repo", REPO, "--json", "name"))
    if not any(secret["name"] == "GEMINI_API_KEY" for secret in names):
        raise RuntimeError("private worker GEMINI_API_KEY is not registered")
    source = args.input.resolve(strict=True)
    settings = json.loads(args.config.read_text()) if args.config else {}
    settings["gemini_model"] = args.gemini_model
    if args.clips is not None:
        settings["clips"] = args.clips
    clips = settings.get("clips", 3)
    if args.chat_url:
        settings["chat_url"] = args.chat_url
    if not re.fullmatch(r"(?:models/)?[A-Za-z0-9._-]+", args.gemini_model):
        raise RuntimeError("invalid Gemini model identifier")
    if type(clips) is not int or not 1 <= clips <= 10:
        raise RuntimeError("clip count must be between 1 and 10")
    maximum = settings.get("max_input_bytes", 1073741824)
    if (
        not source.is_file()
        or not isinstance(maximum, int)
        or not 0 < source.stat().st_size <= min(maximum, 2147483648)
    ):
        raise RuntimeError("video must be a regular file within the input size limit")
    if args.chat_file and (
        not args.chat_file.is_file() or args.chat_file.stat().st_size > 20000000
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
    }
    receipt_path = state / "receipt.json"
    save(receipt_path, receipt)
    print("Private job receipt: " + str(receipt_path), flush=True)
    request = {
        "request_id": identity,
        "source_ref": sha,
        "video_sha256": digest(source),
        "video_bytes": source.stat().st_size,
        "settings": settings,
        "chat_sha256": digest(args.chat_file) if args.chat_file else None,
    }
    with tempfile.TemporaryDirectory(prefix="clipforge-submit-") as temporary:
        directory = Path(temporary)
        video = directory / "input-video.bin"
        video.symlink_to(source)
        manifest = directory / "request.json"
        manifest.write_text(json.dumps(request))
        files = [str(video), str(manifest)]
        if args.chat_file:
            chat = directory / "chat.json"
            chat.symlink_to(args.chat_file.resolve())
            files.append(str(chat))
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
            "--notes",
            "Temporary private ClipForge job input. Automatically cleaned after execution.",
            timeout=1800,
        )
    receipt["status"] = "STAGED"
    save(receipt_path, receipt)
    payload = json.dumps(
        {
            "ref": "main",
            "inputs": {"request_id": identity, "input_tag": tag, "source_ref": sha},
        }
    )
    gh(
        "api",
        "--method",
        "POST",
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        "--input",
        "-",
        body=payload,
    )
    receipt["status"] = "DISPATCHED"
    save(receipt_path, receipt)
    print("Submitted private ClipForge job " + identity, flush=True)
    deadline = time.monotonic() + 6000
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
                    "status,conclusion",
                )
            )
            if run["status"] == "completed":
                if run["conclusion"] != "success":
                    receipt.update(status="FAILED", conclusion=run["conclusion"])
                    save(receipt_path, receipt)
                    raise RuntimeError(
                        "private job did not complete successfully; no retry or fallback"
                    )
                destination = args.output or state / "result"
                if destination.exists():
                    raise RuntimeError(
                        "download destination already exists; choose a new directory"
                    )
                gh(
                    "run",
                    "download",
                    str(run_id),
                    "--repo",
                    REPO,
                    "--name",
                    "clipforge-result-" + identity,
                    "--dir",
                    str(destination),
                    timeout=300,
                )
                returned = json.loads(
                    (destination / "request-receipt.json").read_text()
                )
                if (
                    returned.get("request_id") != identity
                    or returned.get("source_ref") != sha
                    or returned.get("status") != "complete"
                ):
                    raise RuntimeError("result identity or completion state mismatch")
                manifest = json.loads((destination / "manifest.json").read_text())
                if (
                    manifest.get("status") != "complete"
                    or manifest.get("source_sha256") != request["video_sha256"]
                ):
                    raise RuntimeError("result manifest does not match the input")
                rendered = manifest.get("rendered", [])
                if not 1 <= len(rendered) <= clips or len(rendered) != returned.get(
                    "rendered_clips"
                ):
                    raise RuntimeError("result clip count mismatch")
                for clip in rendered:
                    relative = clip["directory"]
                    if not re.fullmatch(r"clips/clip-[0-9]{3}", relative):
                        raise RuntimeError("unexpected output directory")
                    checksums = clip.get("sha256", {})
                    if set(checksums) != {"video.mp4", "captions.ass", "captions.srt"}:
                        raise RuntimeError("missing output checksums")
                    for name, checksum in checksums.items():
                        target = destination / relative / name
                        if target.is_symlink() or digest(target) != checksum:
                            raise RuntimeError("downloaded output checksum mismatch")
                receipt.update(status="COMPLETE", output=str(destination.resolve()))
                save(receipt_path, receipt)
                print("Completed; private outputs downloaded.")
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
