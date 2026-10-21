#!/usr/bin/env python3
"""Submit a reviewed production plan through a private, temporary input release."""

import argparse
import fcntl
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from clipforge.batch_models import ProductionPlan
from clipforge.storage import PipelineError, digest, fingerprint, write_json

PRIVATE_REPO = "nyu1791-collab/ai-model-workers"
WORKFLOW = "clipforge-rich-batch.yml"


def gh(*args):
    result = subprocess.run(
        ["gh", *map(str, args)], capture_output=True, text=True, timeout=180
    )
    if result.returncode:
        raise PipelineError(
            "private batch operation failed; inspect submission receipt before retrying",
            code="private_submission",
        )
    return result.stdout


def submit(path, source_ref, cache_run_id="", state=Path("workspace/control/batches")):
    path = Path(path)
    if path.stat().st_size > 8 * 1024**2 or not re.fullmatch(
        r"[a-f0-9]{40}", source_ref
    ):
        raise ValueError("invalid plan size or source commit")
    if cache_run_id and not re.fullmatch(r"[0-9]+", cache_run_id):
        raise ValueError("cache run must be numeric")
    plan = ProductionPlan.model_validate_json(path.read_text())
    if not json.loads(gh("api", f"repos/{PRIVATE_REPO}"))["private"]:
        raise PipelineError("designated worker repository must be private")
    key = fingerprint({"plan": digest(path), "source_ref": source_ref})
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = state / (key + ".json")
    with (state / (key + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineError("same plan submission is in progress") from None
        if receipt.exists():
            return json.loads(receipt.read_text())
        tag = "clipforge-manifest-" + uuid.uuid4().hex
        record = {
            "status": "preparing",
            "input_tag": tag,
            "repository": PRIVATE_REPO,
            "workflow": WORKFLOW,
            "source_ref": source_ref,
            "plan_sha256": digest(path),
            "cache_run_id": cache_run_id,
            "review_basis": plan.review_basis,
        }
        write_json(receipt, record)
        try:
            with tempfile.TemporaryDirectory(prefix="clipforge-plan-") as d:
                fixed = Path(d) / "production_plan.json"
                write_json(fixed, plan.model_dump())
                gh(
                    "release",
                    "create",
                    tag,
                    fixed,
                    "--repo",
                    PRIVATE_REPO,
                    "--title",
                    "Private reviewed ClipForge input",
                    "--notes",
                    "Temporary private input; remove after the linked worker finishes.",
                )
            record["status"] = "dispatching"
            write_json(receipt, record)
            gh(
                "workflow",
                "run",
                WORKFLOW,
                "--repo",
                PRIVATE_REPO,
                "-f",
                f"source_ref={source_ref}",
                "-f",
                "mode=reviewed",
                "-f",
                f"manifest_release={tag}",
                "-f",
                f"cache_run_id={cache_run_id}",
            )
            record["status"] = "submitted"
            record["workflow_url"] = (
                f"https://github.com/{PRIVATE_REPO}/actions/workflows/{WORKFLOW}"
            )
            write_json(receipt, record)
            return record
        except Exception:
            # A network failure after dispatch may still have started a job. Keep
            # the unique tag/receipt and never automatically send it a second time.
            record["status"] = "submission_uncertain"
            write_json(receipt, record)
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--source-ref", required=True)
    p.add_argument("--cache-run-id", default="")
    a = p.parse_args()
    try:
        print(json.dumps(submit(a.plan, a.source_ref, a.cache_run_id)))
        return 0
    except Exception as e:
        print(
            json.dumps(
                {
                    "status": "submission_failed",
                    "error_type": type(e).__name__,
                    "error_code": getattr(e, "code", None),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
