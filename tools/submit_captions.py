#!/usr/bin/env python3
"""Recognise reviewed clip intervals with Parakeet on the private worker; print the run ID.

The run's clipforge-captions artifact is then passed to a render with
`submit_audio_stage.py --stage render --captions-run RUN_ID`.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

REPO = "nyu1791-collab/ai-model-workers"
WORKFLOW = "clipforge-captions.yml"


class CaptionsError(RuntimeError):
    pass


def gh(*arguments):
    result = subprocess.run(
        ["gh", *arguments], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise CaptionsError("GitHub operation failed: " + arguments[0])
    return result.stdout


def review_payload(path):
    """The approved review without any attached captions, as the workflow input."""
    value = json.loads(Path(path).read_text())
    clips = value.get("clips") if isinstance(value, dict) else None
    if (
        not isinstance(clips, list)
        or not clips
        or any(
            not isinstance(c, dict) or c.get("video_reviewed") is not True
            for c in clips
        )
    ):
        raise CaptionsError("each selected topic requires video review")
    value = {
        **value,
        "clips": [{k: v for k, v in c.items() if k != "captions"} for c in clips],
    }
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode()) > 60000:
        raise CaptionsError("review metadata is oversized")
    return text


def dispatch(previous_run, source_ref, review):
    if not re.fullmatch(r"[0-9]{1,20}", previous_run):
        raise CaptionsError("--previous-run must be a numeric run ID")
    if not re.fullmatch(r"[a-f0-9]{40}", source_ref):
        raise CaptionsError("--source-ref must be a full commit SHA")
    path = f"repos/{REPO}/actions/workflows/{WORKFLOW}"
    before = {
        run["id"]
        for run in json.loads(gh("api", f"{path}/runs?per_page=20"))["workflow_runs"]
    }
    gh(
        "api", "-X", "POST", f"{path}/dispatches",
        "-f", "ref=main",
        "-f", f"inputs[previous_run_id]={previous_run}",
        "-f", f"inputs[source_ref]={source_ref}",
        "-f", f"inputs[review_json]={review}",
    )  # fmt: skip
    for _ in range(30):
        runs = json.loads(gh("api", f"{path}/runs?per_page=20"))["workflow_runs"]
        new = [run["id"] for run in runs if run["id"] not in before]
        if new:
            return max(new)
        time.sleep(4)
    raise CaptionsError("the captions run did not appear; check the worker Actions")


def wait(run_id, poll=30):
    while True:
        run = json.loads(gh("api", f"repos/{REPO}/actions/runs/{run_id}"))
        if run["status"] == "completed":
            return run["conclusion"]
        time.sleep(poll)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-run", required=True, help="analysis run ID")
    parser.add_argument("--source-ref", required=True, help="reviewed ClipForge SHA")
    parser.add_argument("--review-file", required=True, type=Path)
    parser.add_argument("--wait", action="store_true", help="wait for the run to end")
    args = parser.parse_args(argv)
    run_id = dispatch(
        args.previous_run, args.source_ref, review_payload(args.review_file)
    )
    result = {"captions_run_id": str(run_id), "status": "DISPATCHED"}
    if args.wait:
        result["status"] = wait(run_id).upper()
    print(json.dumps(result), flush=True)
    return 0 if result["status"] in ("DISPATCHED", "SUCCESS") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CaptionsError as error:
        print("Captions request failed: " + str(error), file=sys.stderr)
        sys.exit(1)
