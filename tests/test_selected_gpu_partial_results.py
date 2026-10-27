"""Selected-ASR failure handling with synthetic files and fake workers only."""

from contextlib import contextmanager
import importlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest


@pytest.fixture
def selected(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    module = importlib.import_module("selected_gpu_asr")

    def no_network(*args, **kwargs):
        raise AssertionError("network disabled")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    return module


def test_partial_workers_keep_valid_saved_results_without_retry(
    selected, tmp_path, monkeypatch
):
    plan = {
        "input_key": "synthetic-parent",
        "clips": [{"start": i, "end": i + 1} for i in range(3)],
        "recognizer": "synthetic",
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    calls = []

    def pending(key, order):
        path = tmp_path / f"pending-{order}"
        path.touch()
        return path

    def worker(command, env):
        part_dir = Path(command[command.index("--output") + 1])
        part = int(part_dir.name)
        calls.append(part)
        if part < 2:
            part_plan = json.loads((part_dir / "plan.json").read_text())
            selected.write_json(
                part_dir / "selected-gpu-asr.json",
                {
                    "status": "complete",
                    "input_key": part_plan["input_key"],
                    "clips": [{"segments": []}],
                },
            )
        # Part 1 saves a valid result but its controller reports failure afterward.
        # Part 2 fails before saving any result.
        return SimpleNamespace(returncode=0 if part == 0 else 1)

    monkeypatch.setattr(selected, "register_pending", pending)
    monkeypatch.setattr(selected.subprocess, "run", worker)
    with pytest.raises(selected.ControlError, match="did not all complete"):
        selected.run_parallel(tmp_path / "synthetic.wav", plan_path, tmp_path)
    assert sorted(calls) == [0, 1, 2]
    assert (tmp_path / "selected-gpu-clip-1.json").is_file()
    assert (tmp_path / "selected-gpu-clip-2.json").is_file()
    assert not (tmp_path / "selected-gpu-clip-3.json").exists()
    state = json.loads((tmp_path / "selected-gpu-asr-state.json").read_text())
    assert state["status"] == "gpu_requests_or_unknown"
    assert state["parts"][1]["error_type"] == "ControlError"
    assert state["parts"][2]["error_type"] == "ControlError"
    assert not (tmp_path / "selected-gpu-asr.json").exists()


def test_failed_worker_never_reuses_saved_result_with_wrong_identity(
    selected, tmp_path, monkeypatch
):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"input_key": "synthetic-parent", "clips": [{}, {}]})
    )

    def pending(key, order):
        path = tmp_path / f"pending-{order}"
        path.touch()
        return path

    def worker(command, env):
        part_dir = Path(command[command.index("--output") + 1])
        selected.write_json(
            part_dir / "selected-gpu-asr.json",
            {"status": "complete", "input_key": "wrong", "clips": [{}]},
        )
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(selected, "register_pending", pending)
    monkeypatch.setattr(selected.subprocess, "run", worker)
    with pytest.raises(selected.ControlError, match="did not all complete"):
        selected.run_parallel(tmp_path / "synthetic.wav", plan_path, tmp_path)
    assert not list(tmp_path.glob("selected-gpu-clip-*.json"))


def test_runtime_failure_records_phase_and_keeps_unconfirmed_gpu_reserved(
    selected, tmp_path, monkeypatch
):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "input_key": "synthetic-parent",
                "clips": [{"start": 0, "end": 1}],
                "language": "ja",
            }
        )
    )
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", "a" * 40)
    monkeypatch.setenv("REQUEST_ID", "b" * 32)
    marker = tmp_path / "pending"
    marker.touch()
    monkeypatch.setenv("CLIPFORGE_GPU_PENDING", str(marker))

    def audio(source, clips, work):
        path = work / "audio.wav"
        path.write_bytes(b"synthetic")
        return path, [{"start": 0, "end": 1}]

    class Controller:
        def __init__(self, root):
            self.state_path = root / "state.json"
            self.state_path.parent.mkdir()
            self.state_path.write_text("{}")

        def start(self, mode):
            raise ValueError("private failure detail")

        def stop(self):
            raise RuntimeError("private stop detail")

    class Lease:
        slot_index = 0
        confirmed = False

        def mark_requested(self):
            pass

        def confirm_release(self):
            self.confirmed = True

    lease = Lease()

    @contextmanager
    def slot(*args, **kwargs):
        yield lease

    monkeypatch.setattr(selected, "prepare_audio", audio)
    monkeypatch.setattr(selected, "Controller", Controller)
    monkeypatch.setattr(selected, "gpu_slot", slot)
    monkeypatch.setattr(selected, "github", lambda *args: "true")
    with pytest.raises(ValueError, match="private failure detail"):
        selected.run(tmp_path / "synthetic.wav", plan_path, tmp_path)
    state = json.loads((tmp_path / "selected-gpu-asr-state.json").read_text())
    assert state["status"] == "gpu_requested_or_unknown"
    assert state["failed_phase"] == "running_gpu"
    assert state["error_type"] == "ValueError"
    assert state["stop_error_type"] == "RuntimeError"
    assert not lease.confirmed and "gpu_slot_released_at" not in state
    assert "private failure detail" not in json.dumps(state)
