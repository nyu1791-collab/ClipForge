import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness():
    path = Path(__file__).parents[1] / "tools/dev_harness.py"
    spec = importlib.util.spec_from_file_location("dev_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def task(tmp_path, harness):
    source = tmp_path / "repo"
    source.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        harness.git(source, *args)
    (source / "src/clipforge").mkdir(parents=True)
    (source / "src/clipforge/example.py").write_text("value = 1\n")
    harness.git(source, "add", ".")
    harness.git(source, "commit", "-m", "fixture")
    state = tmp_path / "state"
    workspace = tmp_path / "task"
    job = harness.start(source, "main", workspace, state, "test", "Improve example")
    return state, workspace, job, source


def test_start_preserves_source_and_refuses_existing_workspace(task, harness):
    state, workspace, job, source = task
    assert job["status"] == "ready"
    assert harness.git(source, "branch", "--show-current").strip() == b"main"
    assert harness.git(workspace, "branch", "--show-current").strip() == b"harness/test"
    with pytest.raises(RuntimeError, match="new path"):
        harness.start(source, "main", workspace, state, "second", "goal")
    assert (workspace / "src/clipforge/example.py").read_text() == "value = 1\n"


def test_checks_resume_only_the_same_snapshot(task, harness, monkeypatch):
    state, workspace, job, _ = task
    commands = [["first"], ["second"]]
    monkeypatch.setattr(harness, "CHECKS", commands)
    real_run = subprocess.run
    calls = []
    fail = [True]

    def run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        calls.append(command)
        return SimpleNamespace(
            returncode=1 if command == ["second"] and fail[0] else 0,
            stdout=b"check",
            stderr=b"",
        )

    monkeypatch.setattr(harness.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="check failed"):
        harness.check(state, job)
    assert (
        json.loads((state / "job.json").read_text())["status"]
        == "checks_failed_or_interrupted"
    )
    fail[0] = False
    harness.check(state, job, resume=True)
    assert calls == [["first"], ["second"], ["second"]]
    (workspace / "src/clipforge/example.py").write_text("value = 2\n")
    harness.check(state, job, resume=True)
    assert calls[-2:] == commands


def test_source_modified_by_checks_is_not_approved(task, harness, monkeypatch):
    state, workspace, job, _ = task
    monkeypatch.setattr(harness, "CHECKS", [["modify"]])
    real_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        (workspace / "src/clipforge/example.py").write_text("changed\n")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(harness.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Source changed"):
        harness.check(state, job)
    assert job["status"] != "checks_passed"


@pytest.fixture
def checked(task, harness, monkeypatch):
    state, workspace, job, _ = task
    monkeypatch.setattr(harness, "CHECKS", [])
    harness.check(state, job)
    return state, workspace, job


@pytest.mark.parametrize("bad", [None, "stale", "source", "empty", "binary", "large"])
def test_offline_proposal_import_and_decision(
    checked, harness, tmp_path, monkeypatch, bad
):
    state, workspace, job = checked
    file = tmp_path / "proposal.md"
    file.write_text("Concrete proposal\n")
    commit = job["base_commit"]
    if bad == "stale":
        job["snapshot"] = "different"
    elif bad == "source":
        commit = "wrong"
    elif bad == "empty":
        file.write_text("")
    elif bad == "binary":
        file.write_bytes(b"\x00")
    elif bad == "large":
        file.write_text("x" * 200001)
    real_run = subprocess.run

    def run(command, **kwargs):
        assert command[0] == "git", (
            "Offline harness must never invoke an external model"
        )
        return real_run(command, **kwargs)

    monkeypatch.setattr(harness.subprocess, "run", run)
    before = harness.snapshot(workspace)
    if bad:
        with pytest.raises(RuntimeError):
            harness.proposal(state, job, commit, "Owner supplied proposal", file)
        assert not job.get("proposals")
    else:
        harness.proposal(state, job, commit, "Owner supplied proposal", file)
        assert job["status"] == "proposal_received"
        assert job["proposals"][0]["origin_verified"] is False
        harness.decide(
            state, job, "accept", "Primary will implement after verification"
        )
        assert job["decisions"][0]["decision"] == "accept"
    assert harness.snapshot(workspace) == before


@pytest.mark.parametrize(
    "bad", [None, "sha", "workflow", "job", "conclusion", "pending"]
)
def test_ci_receipts_match_source_workflows_and_all_jobs(
    task, harness, monkeypatch, bad
):
    state, _, job, _ = task
    real_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        quality = command[3] == "1"
        data = {
            "headSha": "wrong" if bad == "sha" else job["base_commit"],
            "status": "in_progress" if bad == "pending" else "completed",
            "conclusion": "failure" if bad == "conclusion" else "success",
            "workflowName": "Wrong"
            if bad == "workflow"
            else "Quality"
            if quality
            else "Build package",
            "jobs": [
                {"name": name, "conclusion": "skipped" if bad == "job" else "success"}
                for name in (
                    ["unit (3.11)", "unit (3.12)", "video"] if quality else ["build"]
                )
            ],
            "url": "https://example.invalid/run",
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(data).encode())

    monkeypatch.setattr(harness.subprocess, "run", run)
    if bad:
        with pytest.raises(RuntimeError):
            harness.check_ci(state, job, "owner/repo", 1, 2)
        assert job["status"] == "checks_failed_or_interrupted"
    else:
        harness.check_ci(state, job, "owner/repo", 1, 2)
        assert job["status"] == "checks_passed" and len(job["checks"]) == 2
