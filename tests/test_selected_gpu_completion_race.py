"""Exercise saved ASR result handoff without GPU, models or external connections."""

import json
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from clipforge import audio_understanding, storage


@pytest.mark.parametrize("exit_code", [0, 1])
def test_successful_parts_are_not_lost_when_worker_exits_during_poll(
    tmp_path, monkeypatch, exit_code
):
    def no_network(*args, **kwargs):
        raise AssertionError("network is forbidden")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("CLIPFORGE_COLAB_CONTROL_PYTHON", sys.executable)
    callbacks = []
    clips = [SimpleNamespace(start=0, end=10), SimpleNamespace(start=10, end=20)]

    def start(*args, **kwargs):
        key = json.loads((tmp_path / "selected-gpu-asr-plan.json").read_text())[
            "input_key"
        ]

        def write_part(index):
            storage.write_json(
                tmp_path / f"selected-gpu-clip-{index}.json",
                {
                    "status": "complete",
                    "input_key": key,
                    "clip_index": index,
                    "transcript": {"segments": [{"text": f"synthetic-{index}"}]},
                },
            )

        write_part(1)

        class Process:
            def wait(self, timeout):
                # Published after the last polling scan, just before process exit.
                write_part(2)
                if not exit_code:
                    storage.write_json(
                        tmp_path / "selected-gpu-asr.json",
                        {"status": "complete", "input_key": key, "clips": [{}, {}]},
                    )
                return exit_code

        return Process()

    monkeypatch.setattr(subprocess, "Popen", start)

    def invoke():
        return audio_understanding.selected_gpu_transcripts(
            tmp_path / "synthetic.wav",
            clips,
            SimpleNamespace(language="ja"),
            tmp_path,
            None,
            False,
            "synthetic-sha",
            on_clip=lambda index, value: callbacks.append(index),
        )

    if exit_code:
        with pytest.raises(storage.PipelineError, match="did not complete"):
            invoke()
    else:
        result, reused = invoke()
        assert result["status"] == "complete" and not reused
    assert callbacks == [1, 2]
