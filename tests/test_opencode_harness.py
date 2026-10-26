import fnmatch
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
    monkeypatch.setattr(
        module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=module.MIN_FREE_BYTES),
    )
    # Tests never launch OpenCode, so the binary and credential only have to be named.
    credential = tmp_path / "synthetic-credential.json"
    credential.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credential))
    monkeypatch.setenv("CLIPFORGE_OPENCODE_BIN", "/synthetic/opencode")

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


@pytest.mark.parametrize("status", ["complete", "failed"])
def test_run_removes_home_and_keeps_reports(harness, task, monkeypatch, status):
    def attempt(command, environment, workdir, out, timeout, result, texts, **options):
        home = Path(environment["HOME"])
        assert home.is_dir()
        cache = home / ".cache" / "opencode"
        cache.mkdir(parents=True)
        (cache / "synthetic-cache").write_bytes(b"synthetic data")
        out.write('{"type":"synthetic"}\n')
        texts.append("Synthetic report.")
        result["status"] = status
        if status != "complete":
            result["error"] = "opencode exited 1"

    monkeypatch.setattr(harness, "attempt", attempt)
    result = harness.run_task(task)

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
    assert result["status"] == status


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
        harness.run_task(task)

    assert len(homes) == 1
    assert not homes[0].exists()
    assert (homes[0].parent / "opencode.json").is_file()
    assert (homes[0].parent / "events.jsonl").is_file()


def test_missing_credential_refuses_before_any_request(harness, task, monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS")
    monkeypatch.setattr(
        harness, "attempt", lambda *args, **options: pytest.fail("must not launch")
    )

    with pytest.raises(RuntimeError, match="Vertex ADC is not connected"):
        harness.run_task(task)


@pytest.mark.parametrize("link_target", ["profile", "run"])
def test_cleanup_warns_and_preserves_success_when_home_resolves_outside_run_or_to_run(
    harness, task, tmp_path, monkeypatch, link_target
):
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    marker = profile / "keep.txt"
    marker.write_text("synthetic profile data", encoding="utf-8")

    def run(task, timeout, directory):
        target = profile if link_target == "profile" else directory
        (directory / "home").symlink_to(target, target_is_directory=True)
        result = {"status": "complete", "report": "synthetic report"}
        (directory / "result.json").write_text(json.dumps(result))
        return result

    monkeypatch.setattr(harness, "_run_task", run)
    result = harness.run_task(task)

    assert marker.read_text() == "synthetic profile data"
    directory = next((harness.STATE / "runs").iterdir())
    assert (directory / "home").is_symlink()
    assert result["status"] == "complete"
    assert result["report"] == "synthetic report"
    assert "refusing to remove home" in result["cleanup_warning"]
    assert json.loads((directory / "result.json").read_text()) == result


def test_low_disk_space_only_warns(harness, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        harness.shutil, "disk_usage", lambda path: SimpleNamespace(free=1)
    )

    harness.check_disk_space(tmp_path)

    assert "low disk space" in capsys.readouterr().err


def test_report_keeps_every_text_part(harness, task, monkeypatch):
    def attempt(command, environment, workdir, out, timeout, result, texts, **options):
        texts.extend(["First part.", "Final part."])
        result["status"] = "complete"

    monkeypatch.setattr(harness, "attempt", attempt)
    result = harness.run_task(task)

    report = Path(result["report"]).read_text(encoding="utf-8")
    assert report == "First part.\n\nFinal part."


def test_empty_report_says_why(harness, task, monkeypatch):
    seen = {}

    def attempt(command, environment, workdir, out, timeout, result, texts, **options):
        seen.update(options)
        result["error"] = "stalled before the first event"

    monkeypatch.setattr(harness, "attempt", attempt)
    result = harness.run_task(task)

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


def test_progress_lines_hide_credentials_and_stay_short(harness, capsys):
    harness.emit("guard: Authorization: Bearer synthetic-token " + "x" * 400)

    printed = capsys.readouterr().err.strip()
    assert "synthetic-token" not in printed
    assert "[REDACTED]" in printed
    assert len(printed) <= 300


def test_tool_input_summary_is_short(harness):
    part = {"tool": "read", "state": {"input": {"path": "x" * 500}}}

    assert len(harness.tool_input(part)) <= 120


@pytest.mark.parametrize(
    ("command", "verdict"),
    [
        ("cat ../gha-creds-123.json", "deny"),
        ("cat ../../secret.txt", "deny"),
        ("head /proc/self/environ", "deny"),
        ("find . -fprint out.txt", "deny"),
        ("rg --pre sh pattern", "deny"),
        ("sed -i s/a/b/ tools/opencode_harness.py", "deny"),
        ("sed -n 1,5w out.txt tools/opencode_harness.py", "deny"),
        ("grep -rn token .. ", "deny"),
        ("cat README.md", "allow"),
        ("git diff main..HEAD", "allow"),
        ("sed -n 1,20p tools/opencode_harness.py", "allow"),
        ("git grep -n READ_BASH", "allow"),
    ],
)
def test_read_bash_rules_block_writes_and_credential_paths(harness, command, verdict):
    rules = harness.config()["permission"]["bash"]
    # Later rules win, in the order the config lists them.
    decided = "deny"
    for pattern, action in rules.items():
        if fnmatch.fnmatchcase(command, pattern):
            decided = action
    assert decided == verdict
