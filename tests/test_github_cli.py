import hashlib
import json
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import github_cli
from clipforge.storage import PipelineError

SECRET_URL = "https://objects.example.test/file?X-Amz-Signature=SECRET-SIGNATURE"
SECRET_TOKEN = "ghp_SECRETTOKEN123"


@pytest.fixture
def fake_gh(monkeypatch):
    """Replace subprocess.run with a recorder; no real gh process is started."""
    calls = []
    reply = {"returncode": 0, "stdout": "ok\n", "stderr": "", "raise": None}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if reply["raise"] is not None:
            raise reply["raise"]
        return SimpleNamespace(
            returncode=reply["returncode"],
            stdout=reply["stdout"],
            stderr=reply["stderr"],
        )

    monkeypatch.setattr(github_cli.subprocess, "run", run)
    return calls, reply


def test_success_returns_stdout_and_runs_gh_with_arguments(fake_gh):
    calls, _ = fake_gh
    assert github_cli.run("api", "user", "--jq", ".login") == "ok\n"
    command, kwargs = calls[0]
    assert command == ["gh", "api", "user", "--jq", ".login"]
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["input"] is None


def test_body_is_passed_on_stdin(fake_gh):
    calls, _ = fake_gh
    payload = '{"ref": "main", "inputs": {"x": "1"}}'
    github_cli.run("api", "--method", "POST", "repos/o/r/dispatches", body=payload)
    assert calls[0][1]["input"] == payload


def test_timeout_is_passed_to_subprocess(fake_gh):
    calls, _ = fake_gh
    github_cli.run("run", "list", timeout=45)
    assert calls[0][1]["timeout"] == 45


def test_default_timeout_is_applied(fake_gh):
    calls, _ = fake_gh
    github_cli.run("run", "list")
    assert calls[0][1]["timeout"] == github_cli.DEFAULT_TIMEOUT


def test_subprocess_timeout_propagates_unchanged(fake_gh):
    _, reply = fake_gh
    reply["raise"] = subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=5)
    with pytest.raises(subprocess.TimeoutExpired):
        github_cli.run("api", "user", timeout=5)


def test_failure_never_includes_stderr_secrets(fake_gh):
    _, reply = fake_gh
    reply["returncode"] = 1
    reply["stderr"] = f"HTTP 401: Bad credentials {SECRET_URL} token={SECRET_TOKEN}"
    with pytest.raises(RuntimeError) as failure:
        github_cli.run("release", "view", "tag", include_command=True)
    text = str(failure.value)
    assert text == "GitHub operation failed: release"
    for secret in ("SECRET-SIGNATURE", SECRET_TOKEN, "https", "401"):
        assert secret not in text


def test_http_status_is_the_only_stderr_detail_when_requested(fake_gh):
    _, reply = fake_gh
    reply["returncode"] = 1
    reply["stderr"] = f"HTTP 404: Not Found {SECRET_URL}"
    with pytest.raises(RuntimeError) as failure:
        github_cli.run(
            "api",
            "repos/o/r",
            include_command=True,
            include_http_status=True,
        )
    text = str(failure.value)
    assert text == "GitHub operation failed: api (HTTP 404)"
    assert "SECRET-SIGNATURE" not in text and "https" not in text


def test_http_status_absent_from_stderr_adds_nothing(fake_gh):
    _, reply = fake_gh
    reply["returncode"] = 1
    reply["stderr"] = f"network reset {SECRET_URL}"
    with pytest.raises(RuntimeError) as failure:
        github_cli.run("api", include_command=True, include_http_status=True)
    assert str(failure.value) == "GitHub operation failed: api"


def test_custom_message_and_error_type_are_used(fake_gh):
    _, reply = fake_gh
    reply["returncode"] = 1
    reply["stderr"] = SECRET_URL
    with pytest.raises(PipelineError) as failure:
        github_cli.run(
            "run",
            "view",
            error_type=PipelineError,
            message="private analysis artifact operation failed",
        )
    assert str(failure.value) == "private analysis artifact operation failed"
    assert "SECRET-SIGNATURE" not in str(failure.value)


def test_default_failure_is_runtime_error_without_command_name(fake_gh):
    _, reply = fake_gh
    reply["returncode"] = 2
    with pytest.raises(RuntimeError) as failure:
        github_cli.run("secret", "list")
    assert type(failure.value) is RuntimeError
    assert str(failure.value) == "GitHub operation failed"


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a/../../escape", "a\\escape"]
)
def test_result_archive_rejects_unsafe_members(tmp_path, name):
    archive = tmp_path / "result.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, "synthetic")
    with pytest.raises(RuntimeError, match="unsafe"):
        github_cli._extract_result(archive, tmp_path / "result")
    assert not (tmp_path / "result").exists()


def test_result_archive_rejects_symlinks_and_expansion_budget(tmp_path, monkeypatch):
    archive = tmp_path / "result.zip"
    member = zipfile.ZipInfo("link")
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(member, "outside")
    with pytest.raises(RuntimeError, match="unsafe"):
        github_cli._extract_result(archive, tmp_path / "result")
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("regular", "12345")
    monkeypatch.setattr(github_cli, "MAX_RESULT_BYTES", 4)
    with pytest.raises(RuntimeError, match="budget"):
        github_cli._extract_result(archive, tmp_path / "result")


@pytest.mark.parametrize("mutation", [None, "workflow_run", "sha256", "public_repo"])
def test_missing_artifact_uses_only_the_verified_private_release(
    tmp_path, monkeypatch, mutation
):
    archive = tmp_path / "stored.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("synthetic/result.txt", "synthetic result")
    name = "clipforge-result-" + "a" * 32
    note = {
        "schema": 1,
        "artifact_name": name,
        "workflow_run": "123",
        "source_ref": "b" * 40,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "bytes": archive.stat().st_size,
    }
    if mutation == "workflow_run":
        note[mutation] = "456"
    if mutation == "sha256":
        note[mutation] = "0" * 64
    details = {
        "body": json.dumps(note),
        "assets": [{"name": "result.zip", "size": archive.stat().st_size}],
    }

    def fake(command, **kwargs):
        args = command[1:]
        code, out = 0, ""
        if args[:2] == ["run", "download"]:
            code = 1
        elif args[:1] == ["api"]:
            out = "false" if mutation == "public_repo" else "true"
        elif args[:2] == ["release", "view"]:
            out = json.dumps(details)
        elif args[:2] == ["release", "download"]:
            shutil.copyfile(archive, Path(args[args.index("--dir") + 1]) / "result.zip")
        else:
            raise AssertionError(args)
        return SimpleNamespace(returncode=code, stdout=out, stderr=SECRET_URL)

    monkeypatch.setattr(github_cli.subprocess, "run", fake)
    destination = tmp_path / "result"
    args = (
        "run",
        "download",
        "123",
        "--repo",
        github_cli.PRIVATE_REPO,
        "--name",
        name,
        "--dir",
        str(destination),
    )
    if mutation:
        with pytest.raises(PipelineError, match="verification failed") as error:
            github_cli.run(*args, error_type=PipelineError)
        assert "SECRET-SIGNATURE" not in str(error.value)
        assert not destination.exists()
    else:
        assert github_cli.run(*args) == ""
        assert (destination / "synthetic/result.txt").read_text() == "synthetic result"


def test_release_download_does_not_replace_an_existing_result(tmp_path, monkeypatch):
    destination = tmp_path / "result"
    destination.mkdir()
    (destination / "keep").write_text("keep")
    args = (
        "run",
        "download",
        "123",
        "--repo",
        github_cli.PRIVATE_REPO,
        "--name",
        "clipforge-result-" + "a" * 32,
        "--dir",
        str(destination),
    )
    with pytest.raises(RuntimeError, match="nonempty"):
        github_cli._release_download(args, 120)
    assert (destination / "keep").read_text() == "keep"


def test_result_publication_binds_the_run_and_checksums_the_archive(
    tmp_path, monkeypatch
):
    directory = tmp_path / "output"
    directory.mkdir()
    identity = "a" * 32
    receipt = {"request_id": identity, "workflow_run": "123", "source_ref": "b" * 40}
    (directory / "request-receipt.json").write_text(json.dumps(receipt))
    (directory / "synthetic.txt").write_text("synthetic")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    created = []

    def fake(*args, **kwargs):
        if args[0] == "api":
            return "true"
        assert args[:2] == ("release", "create")
        archive = Path(args[3])
        note = json.loads(Path(args[args.index("--notes-file") + 1]).read_text())
        assert note["workflow_run"] == "123"
        assert note["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
        with zipfile.ZipFile(archive) as source:
            assert source.read("synthetic.txt") == b"synthetic"
        created.append(args[2])
        return ""

    monkeypatch.setattr(github_cli, "run", fake)
    github_cli.publish_result(directory, "clipforge-result-" + identity)
    assert created == ["clipforge-output-result-" + identity]
    receipt["workflow_run"] = "456"
    (directory / "request-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="publishing run"):
        github_cli.publish_result(directory, "clipforge-result-" + identity)
