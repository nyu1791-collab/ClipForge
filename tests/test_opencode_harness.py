from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "tools" / "opencode_harness.py"
    spec = importlib.util.spec_from_file_location("test_opencode_harness_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    # Tests never launch OpenCode, so the binary only has to be named, not installed.
    monkeypatch.setenv("CLIPFORGE_OPENCODE_BIN", "/synthetic/opencode")
    monkeypatch.setattr(module, "slot", lambda model: nullcontext())
    monkeypatch.setattr(
        module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=module.MIN_FREE_BYTES),
    )

    def unexpected_process(*args, **kwargs):
        pytest.fail("tests must not launch subprocesses")

    monkeypatch.setattr(module.subprocess, "Popen", unexpected_process)
    monkeypatch.setattr(module.subprocess, "run", unexpected_process)
    return module


@pytest.fixture
def task(tmp_path):
    path = tmp_path / "synthetic-task.md"
    path.write_text("Review synthetic examples only.", encoding="utf-8")
    return path


@pytest.mark.parametrize("status", ["complete", "failed", "timeout"])
@pytest.mark.parametrize("model", ["deepseek", "sol"])
def test_run_removes_home_and_keeps_reports(
    harness, task, tmp_path, monkeypatch, status, model
):
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    marker = profile / "keep.txt"
    marker.write_text("synthetic profile data", encoding="utf-8")
    monkeypatch.setenv("CLIPFORGE_OPENAI_PROFILE", str(profile))
    monkeypatch.setattr(harness, "openai_profile", lambda: profile)

    def attempt(
        command, environment, workdir, out, timeout, result, texts, free, **options
    ):
        home = Path(environment["HOME"])
        assert home.is_dir()
        cache = home / ".cache" / "opencode"
        cache.mkdir(parents=True)
        (cache / "synthetic-cache").write_bytes(b"synthetic data")
        if model == "sol":
            assert environment["XDG_DATA_HOME"] == str(profile)
            # Removing HOME must not follow nested links into the user's profile.
            (home / "profile-link").symlink_to(profile, target_is_directory=True)
        else:
            assert Path(environment["XDG_DATA_HOME"]).is_relative_to(home)
        out.write('{"type":"synthetic"}\n')
        texts.append("Synthetic report.")
        if status == "complete":
            result["status"] = "complete"
        else:
            result["error"] = "timeout" if status == "timeout" else "opencode exited 1"
        return True

    monkeypatch.setattr(harness, "attempt", attempt)
    result = harness.run_task(task, "read", "HEAD", 30, model)

    directory = Path(result["report"]).parent
    assert not (directory / "home").exists()
    assert {path.name for path in directory.iterdir()} == {
        "events.jsonl",
        "report.md",
        "result.json",
        "opencode.json",
        "stderr.log",
    }
    assert (directory / "report.md").read_text() == "Synthetic report."
    assert (directory / "events.jsonl").read_text() == '{"type":"synthetic"}\n'
    assert json.loads((directory / "result.json").read_text()) == result
    assert result["status"] == ("complete" if status == "complete" else "failed")
    assert marker.read_text() == "synthetic profile data"


@pytest.mark.parametrize("error", [OSError("synthetic failure"), KeyboardInterrupt()])
def test_run_removes_home_when_interrupted(harness, task, monkeypatch, error):
    homes = []

    def attempt(command, environment, *args, **options):
        home = Path(environment["HOME"])
        assert home.is_dir()
        homes.append(home)
        raise error

    monkeypatch.setattr(harness, "attempt", attempt)
    with pytest.raises(type(error)):
        harness.run_task(task, "read", "HEAD", 30)

    assert len(homes) == 1
    assert not homes[0].exists()
    assert (homes[0].parent / "opencode.json").is_file()
    assert (homes[0].parent / "events.jsonl").is_file()


def test_run_removes_home_on_setup_failure(harness, task, monkeypatch):
    def profile_error():
        raise SystemExit("synthetic profile unavailable")

    monkeypatch.setattr(harness, "openai_profile", profile_error)
    with pytest.raises(SystemExit, match="synthetic profile unavailable"):
        harness.run_task(task, "read", "HEAD", 30, "sol")

    directory = next((harness.STATE / "runs").iterdir())
    assert not (directory / "home").exists()
    assert (directory / "opencode.json").is_file()


def test_run_keeps_write_worktree(harness, task, tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    changed = worktree / "synthetic-change.txt"
    changed.write_text("synthetic diff", encoding="utf-8")
    monkeypatch.setattr(harness, "worktree", lambda base, name: worktree)
    monkeypatch.setattr(
        harness.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="")
    )

    def attempt(
        command, environment, workdir, out, timeout, result, texts, free, **options
    ):
        assert workdir == worktree
        result["status"] = "complete"
        texts.append("Synthetic report.")
        return True

    monkeypatch.setattr(harness, "attempt", attempt)
    result = harness.run_task(task, "write", "HEAD", 30)

    assert not (Path(result["report"]).parent / "home").exists()
    assert changed.read_text() == "synthetic diff"


@pytest.mark.parametrize("link_target", ["profile", "run"])
def test_cleanup_warns_and_preserves_success_when_home_resolves_outside_run_or_to_run(
    harness, task, tmp_path, monkeypatch, link_target
):
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    marker = profile / "keep.txt"
    marker.write_text("synthetic profile data", encoding="utf-8")

    def run(task, mode, base, timeout, model, name, directory):
        target = profile if link_target == "profile" else directory
        (directory / "home").symlink_to(target, target_is_directory=True)
        result = {"status": "complete", "report": "synthetic report"}
        (directory / "result.json").write_text(json.dumps(result))
        return result

    monkeypatch.setattr(harness, "_run_task", run)
    result = harness.run_task(task, "read", "HEAD", 30)

    assert marker.read_text() == "synthetic profile data"
    directory = next((harness.STATE / "runs").iterdir())
    assert (directory / "home").is_symlink()
    assert result["status"] == "complete"
    assert result["report"] == "synthetic report"
    assert "refusing to remove home" in result["cleanup_warning"]
    assert json.loads((directory / "result.json").read_text()) == result


@pytest.mark.parametrize("after_slot", [False, True])
def test_low_disk_space_prevents_launch(harness, task, monkeypatch, after_slot):
    checks = []

    def disk_usage(path):
        checks.append(path)
        free = harness.MIN_FREE_BYTES
        if not after_slot or len(checks) > 1:
            free -= 1
        return SimpleNamespace(free=free)

    monkeypatch.setattr(harness.shutil, "disk_usage", disk_usage)
    with pytest.raises(RuntimeError, match="insufficient disk space.*at least 3 GB"):
        harness.run_task(task, "read", "HEAD", 30)

    directories = list((harness.STATE / "runs").iterdir())
    if after_slot:
        assert len(checks) == 2
        assert len(directories) == 1
        assert not (directories[0] / "home").exists()
    else:
        assert directories == []


def test_vertex_waits_longer_and_an_empty_report_says_why(
    harness, task, tmp_path, monkeypatch
):
    credential = tmp_path / "synthetic-credential.json"
    credential.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credential))
    seen = {}

    def attempt(
        command, environment, workdir, out, timeout, result, texts, free, **options
    ):
        seen.update(options)
        result["error"] = "stalled before the first event"
        return False

    monkeypatch.setattr(harness, "attempt", attempt)
    result = harness.run_task(task, "read", "HEAD", 30, "vertex")

    assert seen["stall"] == 300
    assert seen["errors"] is not None
    directory = Path(result["report"]).parent
    assert (directory / "stderr.log").is_file()
    report = (directory / "report.md").read_text(encoding="utf-8")
    assert report.startswith("No model report. status=failed")
    assert "stalled before the first event" in report
    assert "stderr_tail" not in result


def test_stderr_tail_hides_credential_like_text(harness, tmp_path):
    log = tmp_path / "stderr.log"
    log.write_text(
        'Authorization: Bearer synthetic-token\n{"private_key": "synthetic"}\n',
        encoding="utf-8",
    )
    tail = harness.redacted_tail(log)

    assert "synthetic-token" not in tail
    assert '"synthetic"' not in tail
    assert tail.count("[REDACTED]") == 2
