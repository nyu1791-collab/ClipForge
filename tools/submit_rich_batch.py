#!/usr/bin/env python3
"""Submit private interval metadata; the worker restores subtitles from its ASR cache."""

import argparse
import fcntl
import json
import re
import subprocess
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
    if not re.fullmatch(r"[0-9]+", cache_run_id):
        raise PipelineError(
            "reviewed intervals require a complete private ASR cache run",
            code="asr_cache_required",
        )
    plan = ProductionPlan.model_validate_json(path.read_text())
    if any(c.subtitles for m in (plan.shorts, plan.topics) for c in m.clips):
        raise PipelineError(
            "remote submissions accept intervals only; keep edited subtitles on the worker",
            code="interval_metadata_required",
        )
    if not json.loads(gh("api", f"repos/{PRIVATE_REPO}"))["private"]:
        raise PipelineError("designated worker repository must be private")
    key = fingerprint(
        {
            "plan": plan.model_dump(),
            "source_ref": source_ref,
            "cache_run_id": cache_run_id,
        }
    )
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
        submission_id = "clipforge-reviewed-" + uuid.uuid4().hex
        fields = {
            "source_ref": source_ref,
            "mode": "reviewed",
            "submission_id": submission_id,
            "cache_run_id": cache_run_id,
            "plan_json": json.dumps(
                plan.model_dump(), ensure_ascii=False, separators=(",", ":")
            ),
        }
        if len(json.dumps({"inputs": fields}, ensure_ascii=True).encode()) > 60000:
            raise PipelineError(
                "interval metadata exceeds Actions input budget",
                code="plan_input_budget",
            )
        record = {
            "status": "dispatching",
            "submission_id": submission_id,
            "repository": PRIVATE_REPO,
            "workflow": WORKFLOW,
            "source_ref": source_ref,
            "plan_sha256": digest(path),
            "cache_run_id": cache_run_id,
            "review_basis": plan.review_basis,
        }
        write_json(receipt, record)
        args = []
        for name, value in fields.items():
            args += ["-f", f"{name}={value}"]
        try:
            gh("workflow", "run", WORKFLOW, "--repo", PRIVATE_REPO, *args)
            record["status"] = "submitted"
            record["workflow_url"] = (
                f"https://github.com/{PRIVATE_REPO}/actions/workflows/{WORKFLOW}"
            )
            write_json(receipt, record)
            return record
        except Exception:
            # A failed response can follow a successful dispatch. Retain its unique
            # ID and never automatically submit the same input a second time.
            record["status"] = "submission_uncertain"
            write_json(receipt, record)
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--source-ref", required=True)
    p.add_argument("--cache-run-id", required=True)
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
