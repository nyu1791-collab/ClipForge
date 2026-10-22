"""Dispatch one private downstream stage, using its existing analysis artifact."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPO = "nyu1791-collab/ai-model-workers"
BRANCH = "ccr-12fe9485-ugaszt"


def gh(*args, body=None):
    result = subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, timeout=60
    )
    if result.returncode:
        raise RuntimeError("private audio-stage dispatch failed; no automatic retry")
    return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("previews", "render"), required=True)
    parser.add_argument("--previous-run", required=True)
    parser.add_argument("--previous-request", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--review-file", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
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
            "Audio stage submission failed; inspect its private receipt. No retry or fallback was started.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
