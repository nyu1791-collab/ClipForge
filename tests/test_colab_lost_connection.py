"""The SDK's websocket can drop while the kernel keeps rendering; the result decides."""

import importlib
import json
from pathlib import Path
import sys
import types

import pytest

from clipforge import release_transfer

ROOT = Path(__file__).parents[1]
REQUEST = "a" * 32
TAG = "clipforge-result-" + REQUEST + "-run-77"


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def time(self):
        return 1000.0 + self.now


class Runtime:
    def __init__(self, events, error):
        self.events, self.error, self.calls = events, error, 0

    def execute_code(self, code, **kwargs):
        self.calls += 1
        if self.calls > 1:
            raise self.error  # The payload cleanup uses the same dropped connection.
        kwargs["output_hook"]({"output_type": "stream", "text": "private speech\n"})
        self.events.append("execute")
        raise self.error

    def stop(self):
        self.events.append("disconnect")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    control = importlib.import_module("colab_actions")
    monkeypatch.setattr(control, "last_marker", None)
    clock = Clock()
    monkeypatch.setattr(control, "time", clock)
    events = []
    listing = {"content": [{"name": "sample_data"}, {"name": "clipforge-job"}]}

    class Contents:
        def __init__(self, state):
            pass

        def upload(self, local, remote):
            pass

        def list_dir(self, path):
            assert path == "/content"
            return {"content": listing["content"]}

    fake = types.ModuleType("colab_cli")
    contents = types.ModuleType("colab_cli.contents")
    contents.ContentsClient = Contents
    state = types.ModuleType("colab_cli.state")
    state.SessionState = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "colab_cli", fake)
    monkeypatch.setitem(sys.modules, "colab_cli.contents", contents)
    monkeypatch.setitem(sys.modules, "colab_cli.state", state)
    monkeypatch.setenv("GH_TOKEN", "test-only-token")
    controller = control.Controller(tmp_path / "control")
    control.write_private(
        controller.state_path,
        {
            "status": "allocated",
            "mode": "audio-stage",
            "request_id": REQUEST,
            "source_ref": "b" * 40,
            "input_tag": "clipforge-input-" + REQUEST,
            "workflow_run": "77",
            "result_transport": "final-release",
            "stage": {"clip_index": None},
            "endpoint": "owned",
            "url": "https://test.invalid",
            "token": "test-only",
        },
    )
    monkeypatch.setattr(controller, "stop", lambda: events.append("release-runtime"))
    return types.SimpleNamespace(
        control=control,
        controller=controller,
        clock=clock,
        events=events,
        listing=listing,
        output=tmp_path / "output",
        monkeypatch=monkeypatch,
    )


def use_runtime(harness, error):
    runtime = Runtime(harness.events, error)
    harness.monkeypatch.setattr(
        harness.control, "runtime_from_state", lambda record: runtime
    )
    return runtime


def transfer(status="complete"):
    return {
        "request_receipt": {
            "request_id": REQUEST,
            "source_ref": "b" * 40,
            "status": status,
        },
        "stage_manifest": {"status": status},
        "gpu_receipt": {
            "stage": "complete" if status == "complete" else "render",
            "error_type": None if status == "complete" else "PipelineError",
            "timings": {"render": 40.0},
        },
    }


def publish_after(harness, seconds, record):
    """The worker's Release appears once the given time has passed."""

    def release(tag):
        assert tag == TAG
        harness.events.append("poll")
        return {"assets": []} if harness.clock.now >= seconds else None

    harness.monkeypatch.setattr(release_transfer, "release", release)
    harness.monkeypatch.setattr(
        release_transfer, "read_transfer", lambda run, name: record
    )


def test_lost_connection_waits_for_the_live_worker_and_succeeds(harness, capsys):
    # Measured in run 41317576773: the job gave up at 11 s; its Release appeared at about 68 s.
    use_runtime(harness, RuntimeError("Connection was lost."))
    publish_after(harness, 68, transfer())
    harness.controller.run(harness.output)
    printed = capsys.readouterr().out
    assert harness.events.index("release-runtime") > max(
        index for index, event in enumerate(harness.events) if event == "poll"
    )
    assert "Colab execution failed: RuntimeError" in printed
    assert "Colab last marker: None" in printed
    assert "Colab SDK message: Connection was lost." in printed
    assert "Colab stage: complete error: None" in printed
    assert json.loads((harness.output / "manifest.json").read_text())["status"] == (
        "complete"
    )


def test_recovered_worker_failure_is_still_a_failure_and_names_its_stage(
    harness, capsys
):
    use_runtime(harness, RuntimeError("Connection was lost."))
    publish_after(harness, 30, transfer("failed"))
    with pytest.raises(harness.control.ControlError):
        harness.controller.run(harness.output)
    assert "Colab stage: render error: PipelineError" in capsys.readouterr().out


def test_worker_that_never_started_is_not_waited_for(harness, capsys):
    harness.listing["content"] = [{"name": "sample_data"}]
    error = RuntimeError("IOPub channel must be running to receive output")
    use_runtime(harness, error)
    publish_after(harness, 10**9, transfer())

    def missing(run, name):
        raise release_transfer.TransferError("RESULT_NOT_FOUND")

    harness.monkeypatch.setattr(release_transfer, "read_transfer", missing)
    with pytest.raises(RuntimeError) as raised:
        harness.controller.run(harness.output)
    printed = capsys.readouterr().out
    assert raised.value is error
    assert harness.clock.now < 120
    assert "Colab worker never started" in printed
    assert (
        "Colab result unavailable after execution failure: RESULT_NOT_FOUND" in printed
    )


def test_silent_worker_stops_at_the_execution_budget(harness, capsys):
    error = RuntimeError("Connection was lost.")
    use_runtime(harness, error)
    publish_after(harness, 10**9, transfer())

    def missing(run, name):
        raise release_transfer.TransferError("RESULT_NOT_FOUND")

    harness.monkeypatch.setattr(release_transfer, "read_transfer", missing)
    with pytest.raises(RuntimeError) as raised:
        harness.controller.run(harness.output)
    assert raised.value is error
    assert harness.clock.now <= harness.control.EXECUTION_TIMEOUT_SECONDS + 15
    assert "did not appear within the time budget" in capsys.readouterr().out


def test_unreadable_listing_is_not_proof_of_absence(harness, capsys):
    use_runtime(harness, RuntimeError("Connection was lost."))
    publish_after(harness, 300, transfer())
    harness.listing["content"] = []
    harness.controller.run(harness.output)
    assert harness.clock.now >= 300
    assert "never started" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "error_name", ["ControlError", "TimeoutError"], ids=["kernel-error", "time-budget"]
)
def test_reported_failures_do_not_wait(harness, error_name):
    error = (
        harness.control.ControlError("failed")
        if error_name == "ControlError"
        else TimeoutError("budget")
    )
    use_runtime(harness, error)
    publish_after(harness, 0, transfer())
    with pytest.raises(type(error)):
        harness.controller.run(harness.output)
    assert "poll" not in harness.events


def test_private_text_and_unknown_sdk_messages_stay_hidden(harness, capsys):
    secret = "https://private.invalid/?token=hidden"
    use_runtime(harness, RuntimeError(secret))
    publish_after(harness, 15, transfer())
    harness.controller.run(harness.output)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "private speech" not in captured.out + captured.err
    assert "test-only" not in captured.out + captured.err


def test_missing_result_is_named_not_found(monkeypatch):
    monkeypatch.setattr(release_transfer, "release", lambda tag: None)
    with pytest.raises(release_transfer.TransferError, match="^RESULT_NOT_FOUND$"):
        release_transfer.read_transfer("77", "clipforge-result-" + REQUEST)
