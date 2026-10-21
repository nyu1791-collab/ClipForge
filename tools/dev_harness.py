#!/usr/bin/env python3
"""Model-independent development loop with isolated worktrees and verified checkpoints."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

CHECKS = [
    ["uv", "run", "--no-sync", "ruff", "check", "src", "tools", "tests"],
    ["uv", "run", "--no-sync", "ruff", "format", "--check", "src", "tools", "tests"],
    [
        "uv",
        "run",
        "--no-sync",
        "pytest",
        "-m",
        "not integration and not asr",
        "--cov=clipforge",
        "--cov-fail-under=90",
    ],
    ["uv", "lock", "--check"],
    ["uv", "build", "--wheel"],
]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=60
    )
    if result.returncode:
        raise RuntimeError("Git operation failed: " + args[0])
    return result.stdout


def save(state, job):
    temporary = state / "job.tmp"
    temporary.write_text(json.dumps(job, indent=2, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(state / "job.json")


def snapshot(root):
    sha = hashlib.sha256()
    sha.update(git(root, "rev-parse", "HEAD"))
    sha.update(git(root, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"))
    for name in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(
        b"\0"
    ):
        if name:
            path = root / os.fsdecode(name)
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(
                    "Untracked non-regular file; checkpoint it explicitly"
                )
            sha.update(name + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return sha.hexdigest()


def start(source, ref, workspace, state, name, goal):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}", name):
        raise RuntimeError("Use a simple unique task name")
    if ref.startswith("-") or not goal.strip():
        raise RuntimeError("Explicit source ref and nonempty goal are required")
    if workspace.exists() or workspace.is_symlink():
        raise RuntimeError("Workspace must be a new path")
    if state.resolve().is_relative_to(workspace.resolve()):
        raise RuntimeError("State must be outside the task workspace")
    commit = git(source, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    state.mkdir(parents=True, mode=0o700, exist_ok=False)
    job = {
        "version": 1,
        "goal": goal,
        "created_at": now(),
        "base_commit": commit,
        "workspace": str(workspace.absolute()),
        "branch": "harness/" + name,
        "status": "preparing",
        "checks": [],
        "proposals": [],
    }
    save(state, job)
    try:
        git(
            source,
            "worktree",
            "add",
            "-b",
            job["branch"],
            str(workspace.absolute()),
            commit,
        )
        job["status"] = "ready"
    except Exception:
        job["status"] = "failed_setup"
        raise
    finally:
        save(state, job)
    return job


def check(state, job, resume=False):
    root = Path(job["workspace"])
    before = snapshot(root)
    old = job.get("checks", []) if resume and job.get("snapshot") == before else []
    job.update(status="checking", snapshot=before, checks=[], check_kind="local")
    save(state, job)
    for index, command in enumerate(CHECKS):
        if (
            index < len(old)
            and old[index].get("command") == command
            and old[index].get("returncode") == 0
        ):
            job["checks"].append(old[index])
            continue
        receipt = {
            "command": command,
            "started_at": now(),
            "returncode": None,
            "host": platform.node(),
            "python": platform.python_version(),
        }
        job["checks"].append(receipt)
        save(state, job)
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, timeout=900)
            (state / f"check-{index}.log").write_bytes(
                (result.stdout + result.stderr)[-2000000:]
            )
            receipt.update(returncode=result.returncode, completed_at=now())
            if result.returncode:
                raise RuntimeError(
                    "Development check failed; inspect check log " + str(index)
                )
            if snapshot(root) != before:
                raise RuntimeError("Source changed during checks; rerun all checks")
        except BaseException:
            job["status"] = "checks_failed_or_interrupted"
            save(state, job)
            raise
        save(state, job)
    if snapshot(root) != before:
        job["status"] = "checks_failed_or_interrupted"
        save(state, job)
        raise RuntimeError("Source changed during checks; rerun all checks")
    job["status"] = "checks_passed"
    save(state, job)
    return job


def check_ci(state, job, repository, quality_run, build_run):
    root = Path(job["workspace"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Use an explicit GitHub owner/repository")
    if git(root, "status", "--porcelain").strip():
        raise RuntimeError("CI receipts require a clean source checkpoint")
    before = snapshot(root)
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    job.update(status="checking", snapshot=before, checks=[], check_kind="ci")
    save(state, job)
    try:
        for run_id, workflow, expected_jobs in (
            (quality_run, "Quality", {"unit (3.11)", "unit (3.12)", "video"}),
            (build_run, "Build package", {"build"}),
        ):
            command = [
                "gh",
                "run",
                "view",
                str(run_id),
                "--repo",
                repository,
                "--json",
                "headSha,status,conclusion,workflowName,jobs,url",
            ]
            response = subprocess.run(command, capture_output=True, timeout=60)
            if response.returncode:
                raise RuntimeError("Unable to verify GitHub CI receipt")
            run = json.loads(response.stdout)
            passed = {
                j["name"]
                for j in run.get("jobs", [])
                if j.get("conclusion") == "success"
            }
            if (
                run.get("headSha") != commit
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"
                or run.get("workflowName") != workflow
                or not expected_jobs <= passed
            ):
                raise RuntimeError(
                    "CI workflow, source commit or required jobs do not match"
                )
            job["checks"].append(
                {
                    "command": command,
                    "returncode": 0,
                    "run_id": run_id,
                    "url": run.get("url"),
                    "source_commit": commit,
                    "verified_at": now(),
                    "passed_jobs": sorted(passed),
                }
            )
            save(state, job)
        if snapshot(root) != before:
            raise RuntimeError("Source changed during CI verification")
        job["status"] = "checks_passed"
    except BaseException:
        job["status"] = "checks_failed_or_interrupted"
        raise
    finally:
        save(state, job)
    return job


def proposal(state, job, source_commit, author, file):
    root = Path(job["workspace"])
    current = snapshot(root)
    if (
        job.get("status") != "checks_passed"
        or job.get("snapshot") != current
        or len(job["checks"]) != (2 if job.get("check_kind") == "ci" else len(CHECKS))
        or any(c.get("returncode") != 0 for c in job["checks"])
    ):
        raise RuntimeError(
            "Current source must pass all checks before importing a proposal"
        )
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    if git(root, "status", "--porcelain").strip() or source_commit != commit:
        raise RuntimeError("Proposal must name the current committed source checkpoint")
    if (
        not author.strip()
        or len(author) > 100
        or not file.is_file()
        or file.stat().st_size > 200000
    ):
        raise RuntimeError(
            "Use an explicit author and a UTF-8 proposal file up to 200KB"
        )
    data = file.read_bytes()
    text = data.decode("utf-8")
    if not text.strip() or "\x00" in text:
        raise RuntimeError("Proposal must contain nonempty UTF-8 text")
    digest = hashlib.sha256(data).hexdigest()
    target = state / ("proposal-" + digest + ".md")
    target.write_bytes(data)
    target.chmod(0o600)
    job.setdefault("proposals", []).append(
        {
            "author": author,
            "source_commit": commit,
            "snapshot": current,
            "sha256": digest,
            "file": str(target.absolute()),
            "imported_at": now(),
            "origin_verified": False,
        }
    )
    job["status"] = "proposal_received"
    save(state, job)
    return job


def decide(state, job, decision, note):
    if job.get("status") != "proposal_received" or snapshot(
        Path(job["workspace"])
    ) != job.get("snapshot"):
        raise RuntimeError("Decision requires a proposal for the current checkpoint")
    job.setdefault("decisions", []).append(
        {
            "decision": decision,
            "note": note,
            "proposal_sha256": job["proposals"][-1]["sha256"],
            "snapshot": job["snapshot"],
            "at": now(),
        }
    )
    save(state, job)
    return job


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start")
    for name in ("source", "workspace", "state"):
        s.add_argument("--" + name, type=Path, required=True)
    for name in ("ref", "name", "goal"):
        s.add_argument("--" + name, required=True)
    for command in ("check", "check-ci", "proposal", "decide", "status"):
        s = sub.add_parser(command)
        s.add_argument("--state", type=Path, required=True)
        if command == "check":
            s.add_argument("--resume", action="store_true")
        if command == "check-ci":
            s.add_argument("--repository", required=True)
            s.add_argument("--quality-run", type=int, required=True)
            s.add_argument("--build-run", type=int, required=True)
        if command == "decide":
            s.add_argument("--decision", choices=["accept", "reject"], required=True)
            s.add_argument("--note", required=True)
        if command == "proposal":
            s.add_argument("--file", type=Path, required=True)
            s.add_argument("--author", required=True)
            s.add_argument("--source-commit", required=True)
    a = p.parse_args()
    if a.command == "start":
        job = start(a.source, a.ref, a.workspace, a.state, a.name, a.goal)
    elif a.command == "status":
        job = json.loads((a.state / "job.json").read_text())
    else:
        with (a.state / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another operation is using this task") from None
            job = json.loads((a.state / "job.json").read_text())
            if a.command == "check":
                job = check(a.state, job, a.resume)
            elif a.command == "check-ci":
                job = check_ci(a.state, job, a.repository, a.quality_run, a.build_run)
            elif a.command == "proposal":
                job = proposal(a.state, job, a.source_commit, a.author, a.file)
            elif a.command == "decide":
                job = decide(a.state, job, a.decision, a.note)
    print(json.dumps(job, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(
            str(error) if isinstance(error, RuntimeError) else type(error).__name__,
            file=sys.stderr,
        )
        sys.exit(1)
