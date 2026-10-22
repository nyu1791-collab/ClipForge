"""Dispatch one private downstream stage, or collect and verify its finished artifact."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

REPO = "nyu1791-collab/ai-model-workers"
BRANCH = "ccr-12fe9485-ugaszt"
WORKFLOW = "clipforge.yml"
RESULT_STATUS = {"previews": "awaiting_video_review", "render": "complete"}
REQUIRED_FILES = {
    "audio.m4a",
    "asr-receipt.json",
    "manifest.json",
    "previews.json",
    "topics.json",
}
MAX_LISTED_FILES = 500
SUBMIT_OPTIONS = (
    "stage",
    "previous_run",
    "previous_request",
    "source_ref",
    "review_file",
)


def gh(*args, body=None, timeout=60):
    result = subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError("private audio-stage GitHub operation failed; no retry")
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


def safe_relative(name):
    """Reject paths that could leave the collected directory or name an unlisted location."""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise RuntimeError("unsafe path in stage receipt")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeError("unsafe path in stage receipt")
    return name


def staged_files(root):
    names = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("stage artifact contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError("stage artifact contains a non-regular entry")
        names.add(path.relative_to(root).as_posix())
    return names


def find_stage_run(identity):
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
            "databaseId,displayTitle,event,headBranch,status,conclusion",
        )
    )
    matches = [
        row
        for row in runs
        if row["displayTitle"] == "ClipForge " + identity
        and row["event"] == "workflow_dispatch"
        and row["headBranch"] == BRANCH
    ]
    if len(matches) != 1:
        raise RuntimeError("expected exactly one private stage run for this request")
    run = matches[0]
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise RuntimeError(
            "private stage run did not complete successfully; no retry or fallback"
        )
    return run["databaseId"]


def verify_stage_artifact(root, identity, stage, request, run_id):
    """Check the worker receipt, every listed SHA-256, extra files and generated receipts."""
    returned = json.loads((root / "request-receipt.json").read_text())
    hashes = returned.get("sha256")
    if (
        returned.get("request_id") != identity
        or returned.get("source_ref") != request["source_ref"]
        or returned.get("audio_stage") != stage
        or returned.get("status") != RESULT_STATUS[stage]
        or returned.get("asr_reused") is not True
        or not returned.get("audio_source_url")
        or str(returned.get("workflow_run")) != str(run_id)
        # The worker receipt does not yet name the previous run; when it does, it must match.
        or str(returned.get("previous_run_id", request["previous_run_id"]))
        != str(request["previous_run_id"])
        or str(returned.get("previous_request_id", request["previous_request_id"]))
        != str(request["previous_request_id"])
        or not isinstance(hashes, dict)
        or not 1 <= len(hashes) <= MAX_LISTED_FILES
        or "request-receipt.json" in hashes
    ):
        raise RuntimeError("stage artifact identity mismatch")
    listed = {safe_relative(name) for name in hashes}
    for value in hashes.values():
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise RuntimeError("invalid SHA-256 entry in stage receipt")
    if not listed >= REQUIRED_FILES:
        raise RuntimeError("stage artifact omits required files")
    found = staged_files(root) - {"request-receipt.json"}
    missing = listed - found
    extra = (
        found - listed - {".audio-first.lock"}
    )  # an empty lock file the worker may leave
    if missing:
        raise RuntimeError(
            str(len(missing)) + " listed file(s) missing from the stage artifact"
        )
    if extra:
        raise RuntimeError(str(len(extra)) + " unlisted file(s) in the stage artifact")
    for name, value in hashes.items():
        if digest(root / name) != value:
            raise RuntimeError("stage artifact SHA-256 mismatch")
    cache = json.loads((root / "asr-receipt.json").read_text())
    key = cache.get("key", "")
    if (
        not cache.get("reused")
        or not isinstance(key, str)
        or not re.fullmatch(r"[a-f0-9]{64}", key)
        or "transcripts/" + key + ".json" not in listed
    ):
        raise RuntimeError("stage ASR identity is incomplete")
    manifest = json.loads((root / "manifest.json").read_text())
    if (
        manifest.get("status") != RESULT_STATUS[stage]
        or manifest.get("audio_stage") != stage
        or manifest.get("source_url") != returned["audio_source_url"]
        or manifest.get("source_sha256") != hashes["audio.m4a"]
    ):
        raise RuntimeError("stage manifest does not match the artifact receipt")
    for name in sorted(listed):
        if name.endswith(".receipt.json"):
            media = name[: -len(".receipt.json")]
            generated = json.loads((root / name).read_text())
            if (
                media not in hashes
                or generated.get("sha256") != hashes[media]
                or generated.get("source_url") != returned["audio_source_url"]
            ):
                raise RuntimeError("generated receipt does not match its media")
    for row in manifest.get("rendered", []):
        directory = safe_relative(row.get("directory"))
        for name, value in row.get("sha256", {}).items():
            if hashes.get(safe_relative(directory + "/" + name)) != value:
                raise RuntimeError("rendered clip receipt does not match its file")
    return len(listed)


def collect(identity, destination):
    """Recover one finished private stage into a new directory; nothing is dispatched."""
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise RuntimeError("invalid request identifier")
    state = Path.home() / ".local/state/clipforge" / identity
    request = json.loads((state / "request.json").read_text())
    receipt = json.loads((state / "receipt.json").read_text())
    stage = request.get("audio_stage")
    if (
        stage not in RESULT_STATUS
        or receipt.get("repository") != REPO
        or receipt.get("worker_ref") != BRANCH
        or receipt.get("request_id") != identity
        or request.get("request_id") != identity
        or request.get("input_tag") != "clipforge-input-" + identity
        or receipt.get("audio_stage") != stage
        or receipt.get("source_ref") != request.get("source_ref")
        or receipt.get("previous_run_id") != request.get("previous_run_id")
        or not re.fullmatch(r"[a-f0-9]{40}", str(request.get("source_ref")))
        or not re.fullmatch(r"[0-9]{1,20}", str(request.get("previous_run_id")))
        or not re.fullmatch(r"[a-f0-9]{32}", str(request.get("previous_request_id")))
    ):
        raise RuntimeError("saved stage request identity mismatch")
    destination = Path(destination).absolute()
    if destination.exists() or any(
        part.is_symlink() for part in [destination, *destination.parents]
    ):
        raise RuntimeError(
            "download destination already exists or uses a symlink; choose a new directory"
        )
    run_id = find_stage_run(identity)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=".clipforge-collect-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "result"
        gh(
            "run",
            "download",
            str(run_id),
            "--repo",
            REPO,
            "--name",
            "clipforge-result-" + identity,
            "--dir",
            str(staging),
            timeout=300,
        )
        count = verify_stage_artifact(staging, identity, stage, request, run_id)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("download destination was created by another operation")
        shutil.move(str(staging), str(destination))
    receipt.update(run_id=run_id, status="COLLECTED", output=str(destination))
    save(state / "receipt.json", receipt)
    print(
        json.dumps(
            {
                "request_id": identity,
                "stage": stage,
                "status": "COLLECTED",
                "verified_files": count,
                "output": str(destination),
            }
        )
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("previews", "render"))
    parser.add_argument("--previous-run")
    parser.add_argument("--previous-request")
    parser.add_argument("--source-ref")
    parser.add_argument("--review-file", type=Path)
    parser.add_argument(
        "--collect",
        metavar="REQUEST_ID",
        help="verify and download one finished stage artifact into a new --output",
    )
    parser.add_argument("--output", type=Path, help="new directory for --collect")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.collect is not None:
        if args.output is None or any(
            getattr(args, name) is not None for name in SUBMIT_OPTIONS
        ):
            parser.error("--collect requires --output and no submission options")
        return collect(args.collect, args.output)
    if args.output is not None:
        parser.error("--output is only used with --collect")
    missing = [
        "--" + name.replace("_", "-")
        for name in ("stage", "previous_run", "previous_request", "source_ref")
        if getattr(args, name) is None
    ]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))
    if (
        not re.fullmatch(r"[0-9]{1,20}", args.previous_run)
        or not re.fullmatch(r"[a-f0-9]{32}", args.previous_request)
        or not re.fullmatch(r"[a-f0-9]{40}", args.source_ref)
    ):
        raise RuntimeError("invalid immutable source or analysis identity")
    review = json.loads(args.review_file.read_text()) if args.review_file else None
    if args.stage == "render" and review is None:
        raise RuntimeError("render requires reviewed topic intervals")
    inputs = {
        "source_ref": args.source_ref,
        "audio_stage": args.stage,
        "previous_run_id": args.previous_run,
        "previous_request_id": args.previous_request,
        "review_json": json.dumps(review, ensure_ascii=False)
        if review is not None
        else "",
    }
    if len(inputs["review_json"].encode()) > 32768:
        raise RuntimeError("review metadata is oversized")
    identity = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[
        :32
    ]
    inputs.update(request_id=identity, input_tag="clipforge-input-" + identity)
    root = Path.home() / ".local/state/clipforge"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".submit.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another ClipForge submission is active") from None
        state = root / identity
        if state.exists():
            raise RuntimeError(
                "this exact stage already has a request; inspect it before retrying"
            )
        if gh("api", "user", "--jq", ".login").strip() != "nyu1791-collab":
            raise RuntimeError("the approved owner connection is required")
        if not json.loads(gh("repo", "view", REPO, "--json", "isPrivate"))["isPrivate"]:
            raise RuntimeError("audio stages require the private worker repository")
        runs = json.loads(
            gh("run", "list", "--repo", REPO, "--limit", "100", "--json", "status")
        )
        if any(row["status"] != "completed" for row in runs):
            raise RuntimeError(
                "another worker is active; no duplicate stage was submitted"
            )
        state.mkdir(mode=0o700)
        (state / "request.json").write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2)
        )
        receipt = {
            "request_id": identity,
            "repository": REPO,
            "source_ref": args.source_ref,
            "worker_ref": BRANCH,
            "status": "DISPATCHING",
            "audio_stage": args.stage,
            "previous_run_id": args.previous_run,
            "job_submitted": None,
        }
        path = state / "receipt.json"
        path.write_text(json.dumps(receipt, indent=2))
        try:
            gh(
                "api",
                "--method",
                "POST",
                f"repos/{REPO}/actions/workflows/clipforge.yml/dispatches",
                "--input",
                "-",
                body=json.dumps({"ref": BRANCH, "inputs": inputs}),
            )
        except Exception:
            receipt["status"] = "DISPATCH_UNCONFIRMED"
            path.write_text(json.dumps(receipt, indent=2))
            raise
        receipt.update(status="DISPATCHED", job_submitted=True)
        path.write_text(json.dumps(receipt, indent=2))
        print(
            json.dumps(
                {"request_id": identity, "stage": args.stage, "status": "DISPATCHED"}
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        print(
            "Audio stage command failed; inspect its private receipt. No retry or fallback was started.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
