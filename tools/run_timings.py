#!/usr/bin/env python3
"""Print the measured timeline of one private worker run: jobs, slow steps, Colab lines.

Reads only the run's job metadata and the public lines the workflows print themselves
(runtime shape, held time, stage timings, compute units); never logs of the media itself.
"""

import argparse
from datetime import datetime
import json
import os
import re
import subprocess

REPO = os.environ.get("CLIPFORGE_ACTIONS_REPO", "nyu1791-collab/ClipForge")
MARKERS = (
    "Colab runtime allocated",
    "Colab refused",
    "Runtime held:",
    "Colab stage:",
    "Compute units",
    "ASR part ",
    "Both ASR parts",
)
STAMP = re.compile(r"^\S+Z ")


def gh(*arguments):
    return subprocess.run(
        ["gh", *arguments], check=True, capture_output=True, text=True, timeout=120
    ).stdout


def seconds(item):
    if not item.get("started_at") or not item.get("completed_at"):
        return None
    start, end = (
        datetime.fromisoformat(item[key].replace("Z", "+00:00"))
        for key in ("started_at", "completed_at")
    )
    return (end - start).total_seconds()


def clock(text):
    return text[11:19] if text else "--:--:--"


def timeline(run_id, repo=REPO, minimum=3.0):
    run = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
    jobs = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs"))["jobs"]
    lines = [
        f"run {run_id} {run.get('conclusion')}: {clock(run.get('run_started_at'))}"
        f"-{clock(run.get('updated_at'))}"
    ]
    for job in jobs:
        took = seconds(job)
        if took is None or job.get("conclusion") == "skipped":
            continue
        lines.append(
            f"job {job['name']}: {clock(job['started_at'])}-{clock(job['completed_at'])}"
            f" {took:.0f} s {job.get('conclusion')}"
        )
        for step in job.get("steps", []):
            took = seconds(step)
            if (
                took is not None
                and took >= minimum
                and step.get("conclusion") != "skipped"
            ):
                lines.append(f"  {took:6.0f} s  {step['name']}")
        log = gh("api", f"repos/{repo}/actions/jobs/{job['id']}/logs")
        for raw in log.splitlines():
            text = STAMP.sub("", raw)
            if text.startswith(MARKERS):
                lines.append(f"  {clock(raw)}  {text}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=int)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--minimum", type=float, default=3.0, help="hide shorter steps")
    args = parser.parse_args(argv)
    print("\n".join(timeline(args.run_id, args.repo, args.minimum)))


if __name__ == "__main__":
    main()
