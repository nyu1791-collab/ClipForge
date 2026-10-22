import subprocess
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
