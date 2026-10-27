"""Observe synthetic existing runs only; no dispatch or external access."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from clipforge import actions_monitor as monitor


@pytest.fixture
def clock(monkeypatch):
    current = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    monkeypatch.setattr(
        monitor,
        "time",
        SimpleNamespace(
            monotonic=lambda: current[0], time=lambda: current[0], sleep=sleep
        ),
    )
    return sleeps


def stage(*, known=True):
    value = {
        "workflow": "clipforge-stages.yml",
        "request_id": "a" * 32,
        "started_at": 90.0,
        "status": "dispatched",
    }
    if known:
        value["run_id"] = 123
    return value


def result(*, title="expected", status="completed", conclusion="success"):
    return json.dumps(
        {
            "displayTitle": title,
            "status": status,
            "conclusion": conclusion,
            "url": "saved-run",
        }
    )


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("known", [False, True])
def test_denied_or_throttled_monitoring_pauses_without_retry_or_resubmission(
    clock, capsys, status, known
):
    saved, calls = [], []
    value = stage(known=known)

    def gh(*args):
        calls.append(args)
        raise RuntimeError(
            f"GitHub operation failed (HTTP {status}) private-URL-and-token"
        )

    with pytest.raises(monitor.MonitoringPending, match=f"HTTP {status}") as error:
        monitor.wait_for_stage(
            value,
            "repository",
            "expected",
            200,
            gh,
            lambda: saved.append(deepcopy(value)),
        )
    assert len(calls) == 1 and calls[0][:2] == ("run", "view" if known else "list")
    assert clock == []
    assert saved[-1]["status"] == "dispatched"
    assert saved[-1]["monitor_status"] == (
        "rate_limited" if status == 429 else "access_denied"
    )
    assert saved[-1]["monitor_http_status"] == status
    assert "private-URL-and-token" not in str(error.value)
    assert "private-URL-and-token" not in capsys.readouterr().out


def test_temporary_read_failure_keeps_run_identity_and_never_dispatches(clock):
    value, calls, saved = stage(), [], []

    def gh(*args):
        assert args[:2] == ("run", "view")
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("GitHub operation failed (HTTP 502)")
        return result()

    assert monitor.wait_for_stage(
        value, "repository", "expected", 200, gh, lambda: saved.append(deepcopy(value))
    ) == ("123", "a" * 32)
    assert len(calls) == 2 and clock == [15]
    assert saved[0]["run_id"] == 123 and saved[0]["status"] == "dispatched"
    assert saved[0]["monitor_status"] == "temporarily_unavailable"
    assert "monitor_error_type" not in saved[-1]


def test_explicit_reconnection_clears_a_previous_access_error(clock):
    value = stage()
    value.update(
        monitor_status="access_denied",
        monitor_http_status=403,
        monitor_error_type="RuntimeError",
    )
    assert monitor.wait_for_stage(
        value, "repository", "expected", 200, lambda *args: result(), lambda: None
    ) == ("123", "a" * 32)
    assert value["status"] == "completed"
    assert "monitor_http_status" not in value and "monitor_error_type" not in value


def test_failed_run_is_not_retried(clock):
    value, calls, saved = stage(), [], []

    def gh(*args):
        calls.append(args)
        return result(conclusion="failure")

    with pytest.raises(RuntimeError, match="Stage failed"):
        monitor.wait_for_stage(
            value,
            "repository",
            "expected",
            200,
            gh,
            lambda: saved.append(deepcopy(value)),
        )
    assert len(calls) == 1 and clock == []
    assert saved[-1]["status"] == "completed" and saved[-1]["conclusion"] == "failure"


def test_different_completed_run_is_never_collected(clock):
    value = stage()
    with pytest.raises(ValueError, match="does not match"):
        monitor.wait_for_stage(
            value,
            "repository",
            "expected",
            200,
            lambda *args: result(title="another request"),
            lambda: None,
        )
    assert value["status"] == "dispatched" and clock == []


def test_multiple_runs_with_same_request_are_never_guessed(clock):
    value = stage(known=False)
    runs = [
        {"databaseId": index, "displayTitle": "expected", "event": "workflow_dispatch"}
        for index in (123, 456)
    ]
    with pytest.raises(ValueError, match="Multiple runs"):
        monitor.wait_for_stage(
            value,
            "repository",
            "expected",
            200,
            lambda *args: json.dumps(runs),
            lambda: None,
        )
    assert "run_id" not in value and clock == []


def test_timeout_keeps_running_remote_work_and_saved_identity(clock):
    value = stage()
    calls, saved = [], []

    def gh(*args):
        calls.append(args)
        return result(status="in_progress", conclusion=None)

    with pytest.raises(monitor.MonitoringPending, match="no resubmission"):
        monitor.wait_for_stage(
            value,
            "repository",
            "expected",
            105,
            gh,
            lambda: saved.append(deepcopy(value)),
        )
    assert len(calls) == 1 and clock == [5]
    assert saved[-1]["run_id"] == 123 and saved[-1]["status"] == "in_progress"
    assert saved[-1]["monitor_status"] == "paused_timeout"
