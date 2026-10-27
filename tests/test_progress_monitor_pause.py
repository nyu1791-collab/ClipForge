"""Synthetic GitHub responses only; no production, model, GPU or network work."""

import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

from clipforge import streaming_render
from clipforge.storage import PipelineError


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network is forbidden in this test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("failure_at", ["release", "transfer"])
def test_streaming_monitor_pauses_once_and_preserves_render_state(
    tmp_path, monkeypatch, capsys, status, failure_at
):
    for name, value in {
        "REQUEST_ID": "a" * 32,
        "CLIPFORGE_SOURCE_REF": "b" * 40,
        "GITHUB_RUN_ID": "123",
        "CLIPFORGE_COLAB_CONTROL_PYTHON": sys.executable,
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if failure_at == "transfer" and len(calls) == 1:
            assets = [
                {"name": name, "state": "uploaded"}
                for name in ("result.zip", "transfer.json")
            ]
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"assets": assets}).encode(), b""
            )
        return subprocess.CompletedProcess(
            args, 1, b"", f"HTTP {status} synthetic-private-token".encode()
        )

    monkeypatch.setattr(streaming_render.subprocess, "run", run)
    renderer = streaming_render.StreamingRenderer(tmp_path, "source", [1, 2], "config")
    renderer.monitor.join(timeout=5)
    assert not renderer.monitor.is_alive()
    with pytest.raises(PipelineError, match=f"HTTP {status}"):
        renderer.close()
    assert len(calls) == (1 if failure_at == "release" else 2)
    state = json.loads((tmp_path / "streaming-render-state.json").read_text())
    assert state["monitor_http_status"] == status
    assert state["monitor_status"] == (
        "rate_limited" if status == 429 else "access_denied"
    )
    assert state["status"] == "complete"  # no render worker was enqueued
    marker = json.loads(
        (tmp_path / "parallel-selected-render" / "monitor-pause.json").read_text()
    )
    assert marker == {"monitor_http_status": status}
    assert "synthetic-private-token" not in capsys.readouterr().out


def test_streaming_monitor_does_not_treat_missing_release_as_denial(
    tmp_path, monkeypatch
):
    for name, value in {
        "REQUEST_ID": "a" * 32,
        "CLIPFORGE_SOURCE_REF": "b" * 40,
        "GITHUB_RUN_ID": "123",
        "CLIPFORGE_COLAB_CONTROL_PYTHON": sys.executable,
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    calls = []
    called = threading.Event()

    def missing(args, **kwargs):
        calls.append(args)
        called.set()
        return subprocess.CompletedProcess(args, 1, b"", b"HTTP 404")

    monkeypatch.setattr(streaming_render.subprocess, "run", missing)
    renderer = streaming_render.StreamingRenderer(tmp_path, "source", [1], "config")
    assert called.wait(5)
    renderer.close()
    assert len(calls) == 1
    assert renderer.monitor_block_status is None
    assert not (tmp_path / "parallel-selected-render" / "monitor-pause.json").exists()


@pytest.mark.parametrize("status", [401, 403, 429])
def test_parallel_progress_pause_waits_for_workers_and_blocks_collection(
    tmp_path, monkeypatch, capsys, status
):
    tool_path = (
        Path(__file__).resolve().parents[1] / "tools/parallel_selected_render.py"
    )
    spec = importlib.util.spec_from_file_location("parallel_render_test", tool_path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps({"selection_basis": "audio_understanding", "clips": [1, 2]})
    )
    for name, value in {
        "REVIEW_FILE": str(review),
        "GITHUB_RUN_ID": "123",
        "REQUEST_ID": "a" * 32,
        "CLIPFORGE_SOURCE_REF": "b" * 40,
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)

    finished, reads, checks = [], [], []

    class Future:
        def result(self):
            finished.append(True)

    class Pool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, *args):
            return Future()

    def wait(pending, **kwargs):
        checks.append(True)
        return (set(), pending) if len(checks) == 1 else (pending, set())

    def read(*args):
        reads.append(args)
        raise RuntimeError(f"HTTP {status} synthetic-private-token")

    monkeypatch.setattr(tool, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(tool, "wait", wait)
    monkeypatch.setattr(tool, "read_transfer", read)
    monkeypatch.setattr(tool, "private_repo", lambda: None)
    monkeypatch.setattr(
        tool, "download", lambda *args, **kwargs: pytest.fail("collection forbidden")
    )
    with pytest.raises(RuntimeError, match="no automatic collection"):
        tool.main()
    assert len(reads) == 1
    assert len(finished) == 2
    marker = tmp_path / "parallel-selected-render" / "monitor-pause.json"
    assert json.loads(marker.read_text()) == {"monitor_http_status": status}
    monkeypatch.setattr(
        tool, "private_repo", lambda: pytest.fail("GitHub check forbidden")
    )
    with pytest.raises(RuntimeError, match="no automatic collection"):
        tool.main(collect_only=True, partial=True)
    assert len(reads) == 1
    assert "synthetic-private-token" not in capsys.readouterr().out
