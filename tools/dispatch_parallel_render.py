#!/usr/bin/env python3
"""Render each reviewed clip on its own GPU run in parallel, then merge the parts.

Each part is a render-only run with clip_index set, so each allocates its own Colab
runtime. When every part has completed, one merge run combines them. A failed part
is reported and never resubmitted here; re-run it explicitly with the same arguments.
State is written before each dispatch so an interrupted session can be reconciled.
"""

import argparse
import json
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = "nyu1791-collab/ClipForge"
WORKFLOW = "clipforge-stages.yml"
PART_RUNS = re.compile(r"[0-9]{1,20}:[a-f0-9]{32}(,[0-9]{1,20}:[a-f0-9]{32}){0,19}")


def gh(*args, body=None):
    result = subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, timeout=120
    )
    if result.returncode:
        raise RuntimeError("GitHub call failed: " + args[0] + " " + args[1])
    return result.stdout


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def part_inputs(request_id, index, count, args):
    return {
        "request_id": request_id,
        "input_tag": "clipforge-input-" + request_id,
        "source_ref": args.source_ref,
        "audio_stage": "render",
        "previous_run_id": str(args.previous_run),
        "previous_request_id": args.previous_request,
        "review_json": "",
        "worker": "colab-auto",
        "clip_index": str(index),
        "part_runs": "",
        "config_file": args.config,
        "result_store": "release",
        "source_url": args.source_url,
        "editorial_release": args.editorial_release,
        "continue_missing": False,
        "auto_render": False,
        "review_approved": args.review_approved,
        "approved_selection_sha256": args.approved_selection_sha256,
        "approved_transcripts_sha256": args.approved_transcripts_sha256,
        "resume_render": False,
        "target_clips": str(count),
    }


def merge_inputs(request_id, part_runs, args):
    return {
        "request_id": request_id,
        "input_tag": "clipforge-input-" + request_id,
        "source_ref": args.source_ref,
        "audio_stage": "merge",
        "previous_run_id": "",
        "previous_request_id": args.previous_request,
        "review_json": "",
        "worker": "cpu",
        "clip_index": "",
        "part_runs": part_runs,
        "config_file": args.config,
        "result_store": "release",
        "source_url": args.source_url,
        "editorial_release": args.editorial_release,
        "continue_missing": False,
        "auto_render": False,
        "resume_render": False,
        "target_clips": str(args.clips),
    }


def plan(args):
    """Part requests for clips 1..N, one per GPU run."""
    parts = []
    for index in range(1, args.clips + 1):
        request_id = uuid.uuid4().hex
        parts.append(
            {
                "clip_index": index,
                "request_id": request_id,
                "inputs": part_inputs(request_id, index, args.clips, args),
            }
        )
    return parts


def dispatch(inputs, args):
    body = json.dumps({"ref": args.ref, "inputs": inputs})
    gh(
        "api",
        "--method",
        "POST",
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        "--input",
        "-",
        body=body,
    )


def find_run(request_id, attempts=24):
    """The run appears a few seconds after dispatch; its title carries the request id."""
    for _ in range(attempts):
        listing = json.loads(
            gh(
                "api",
                f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs",
                "-X",
                "GET",
                "-f",
                "per_page=20",
            )
        )
        for run in listing.get("workflow_runs", []):
            if request_id in (run.get("display_title") or run.get("name") or ""):
                return run["id"]
        time.sleep(5)
    raise RuntimeError("Dispatched run not found for " + request_id)


def run_state(run_id):
    value = gh(
        "api",
        f"repos/{REPO}/actions/runs/{run_id}",
        "--jq",
        '.status + " " + (.conclusion // "-")',
    )
    status, conclusion = value.split()
    return status, conclusion


def wait_for(run_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, conclusion = run_state(run_id)
        if status == "completed":
            return conclusion
        time.sleep(30)
    return "timeout"


def save(state_path, state):
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editorial-release", required=True)
    parser.add_argument("--previous-run", type=int, required=True)
    parser.add_argument("--previous-request", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--clips", type=int, required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--review-approved", action="store_true")
    parser.add_argument("--review-state", type=Path, required=True)
    args = parser.parse_args()
    if not args.review_approved:
        parser.error("explicit review approval is required before rendering")
    from clipforge.review_package import validate_local_package
    from clipforge.media_sources import canonical

    reviewed = json.loads(args.review_state.read_text())
    if (
        reviewed.get("status") != "awaiting_review"
        or reviewed.get("editorial_release") != args.editorial_release
        or canonical(reviewed.get("source_url")) != canonical(args.source_url)
        or str(reviewed.get("review_run_id")) != str(args.previous_run)
        or reviewed.get("review_request_id") != args.previous_request
    ):
        parser.error("render selection does not match the reviewed package")
    hashes = validate_local_package(reviewed, args.review_state)
    args.approved_selection_sha256 = hashes["final-selection.json"]
    args.approved_transcripts_sha256 = hashes["selected-transcripts.json"]
    if not 1 <= args.clips <= 20:
        parser.error("clips must be between 1 and 20")
    if not re.fullmatch(r"config\.[a-z0-9-]+\.example\.json", args.config):
        parser.error("config must name a committed config.*.example.json")
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_ref):
        parser.error("source-ref must be an exact commit SHA")
    if not re.fullmatch(r"[a-f0-9]{32}", args.previous_request):
        parser.error("previous-request must be a 32-character request id")

    parts = plan(args)
    state = {"started_at": now(), "clips": args.clips, "parts": parts, "merge": None}
    if args.dry_run:
        for part in parts:
            print(
                json.dumps(
                    {"clip_index": part["clip_index"], "request_id": part["request_id"]}
                )
            )
        return
    save(args.state, state)
    for part in parts:
        dispatch(part["inputs"], args)
        part["dispatched_at"] = now()
        part["run_id"] = find_run(part["request_id"])
        save(args.state, state)
        print("part", part["clip_index"], "run", part["run_id"], flush=True)

    for part in parts:
        part["conclusion"] = wait_for(part["run_id"], args.timeout)
        save(args.state, state)
        print("part", part["clip_index"], "->", part["conclusion"], flush=True)

    failed = [part["clip_index"] for part in parts if part["conclusion"] != "success"]
    if failed:
        print("not merged; failed parts:", failed, flush=True)
        return
    part_runs = ",".join(f"{part['run_id']}:{part['request_id']}" for part in parts)
    if not PART_RUNS.fullmatch(part_runs):
        raise RuntimeError("part_runs does not match the merge format")
    merge_request = uuid.uuid4().hex
    merge_inputs_value = merge_inputs(merge_request, part_runs, args)
    dispatch(merge_inputs_value, args)
    state["merge"] = {"request_id": merge_request, "dispatched_at": now()}
    state["merge"]["run_id"] = find_run(merge_request)
    save(args.state, state)
    state["merge"]["conclusion"] = wait_for(state["merge"]["run_id"], args.timeout)
    save(args.state, state)
    print(
        "merge",
        state["merge"]["run_id"],
        "->",
        state["merge"]["conclusion"],
        flush=True,
    )


if __name__ == "__main__":
    main()
