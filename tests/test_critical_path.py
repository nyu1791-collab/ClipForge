"""Offline checks for window concurrency, GPU scheduling and private diagnostics."""

from contextlib import contextmanager
import importlib
import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from clipforge import (
    audio_understanding,
    media,
    private_diagnostics,
    storage,
    streaming_render,
)
from clipforge.models import Settings


def test_overview_requests_overlap_and_keep_window_order(tmp_path, monkeypatch):
    audio = tmp_path / "input.m4a"
    audio.write_bytes(b"synthetic input")
    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "synthetic-token")
    monkeypatch.setattr(
        audio_understanding.audio_first,
        "audio_info",
        lambda _: {"duration": 2 * audio_understanding.WINDOW_SECONDS + 120},
    )
    monkeypatch.setattr(
        audio_understanding,
        "encode_audio",
        lambda source, target, *args, **kwargs: target.write_bytes(b"synthetic ogg"),
    )
    barrier = threading.Barrier(3, timeout=5)
    calls = []

    def generate(self, phase, prompt, audio=None, research=False):
        calls.append(phase)
        if phase.startswith("topics-"):
            barrier.wait()
            return {
                "audio_access": "heard",
                "topics": [{"start": 10, "end": 60, "title": phase, "confidence": 1}],
            }, {}
        assert research
        return {"terms": []}, {}

    def selected_gpu(
        audio, clips, settings, output, resume, missing, sha, on_clip=None
    ):
        parts = [{"segments": []} for _ in clips]
        for index, part in enumerate(parts, 1):
            on_clip(index, part)
        return {"clips": parts, "recognizer": "synthetic"}, False

    monkeypatch.setattr(audio_understanding.Client, "generate", generate)
    monkeypatch.setattr(audio_understanding, "selected_gpu_transcripts", selected_gpu)
    output = tmp_path / "out"
    audio_understanding.plan(
        audio,
        "https://kick.com/example/videos/01a171d4-2920-7e7b-a842-e2b580698688",
        output,
        Settings(aspect="16:9", height=360, clips=3),
    )
    assert sorted(x for x in calls if x.startswith("topics-")) == [
        "topics-0",
        "topics-1",
        "topics-2",
    ]
    selected = json.loads((output / "final-selection.json").read_text())
    assert [clip["title"] for clip in selected["clips"]] == [
        "topics-0",
        "topics-1",
        "topics-2",
    ]


def test_ready_clips_get_separate_gpu_workers(tmp_path, monkeypatch):
    for name, value in {
        "REQUEST_ID": "a" * 32,
        "CLIPFORGE_SOURCE_REF": "b" * 40,
        "GITHUB_RUN_ID": "123",
        "CLIPFORGE_COLAB_CONTROL_PYTHON": sys.executable,
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        streaming_render.StreamingRenderer, "_monitor", lambda self: None
    )
    barrier = threading.Barrier(3, timeout=5)
    groups = []

    @contextmanager
    def slot(kind, control_state):
        yield SimpleNamespace(slot_index=0)

    def render(self, worker, completed, state, lease):
        groups.append(tuple(completed))
        barrier.wait()

    monkeypatch.setattr(streaming_render, "gpu_slot", slot)
    monkeypatch.setattr(streaming_render.StreamingRenderer, "render", render)
    renderer = streaming_render.StreamingRenderer(
        tmp_path, "source", [1, 2, 3], "config"
    )
    for index in range(1, 4):
        renderer.enqueue(index, {})
    renderer.finish()
    renderer.close()
    assert sorted(groups) == [(1,), (2,), (3,)]


def test_failed_command_saves_private_redacted_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_PRIVATE_DIAGNOSTIC_DIR", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "synthetic-sensitive-token")
    script = (
        "import sys; "
        "print('failed https://example.invalid/file?auth=abc "
        "token=synthetic-sensitive-token', file=sys.stderr); sys.exit(7)"
    )
    with pytest.raises(storage.PipelineError):
        storage.process([sys.executable, "-c", script], stage="render")
    records = list(tmp_path.glob("command-*.json"))
    assert len(records) == 1
    saved = records[0].read_text()
    assert "synthetic-sensitive-token" not in saved
    assert "example.invalid" not in saved
    value = json.loads(saved)
    assert value["stage"] == "render"
    assert value["returncode"] == 7
    assert "REDACTED" in saved


def test_colab_progress_exposes_only_known_markers(monkeypatch, capsys):
    monkeypatch.syspath_prepend(
        str(__import__("pathlib").Path(__file__).parents[1] / "tools")
    )
    control = importlib.import_module("colab_actions")

    class Runtime:
        def execute_code(self, code, **kwargs):
            hook = kwargs["output_hook"]
            hook(
                {
                    "output_type": "stream",
                    "text": "private speech\nCLIPFORGE_BOOTSTRAP_WORKER\n",
                }
            )
            hook(
                {
                    "output_type": "stream",
                    "text": "CLIPFORGE_DIAG_STAGE_render_RuntimeError",
                }
            )
            return []

    control.checked_execute(Runtime(), "synthetic", 1)
    printed = capsys.readouterr().out
    assert "CLIPFORGE_BOOTSTRAP_WORKER" in printed
    assert "CLIPFORGE_DIAG_STAGE_render_RuntimeError" in printed
    assert "private speech" not in printed


def test_private_diagnostics_redacts_complete_cookie_and_auth_headers():
    lines = private_diagnostics.sanitized_tail(
        "Cookie: first=session-one; second=session-two\n"
        "Authorization: Basic encoded-credentials\n"
        "ordinary ffmpeg diagnostic"
    )
    result = "\n".join(lines)
    assert "session-one" not in result
    assert "session-two" not in result
    assert "encoded-credentials" not in result
    assert "ordinary ffmpeg diagnostic" in result


def test_render_records_ffmpeg_process_start_and_end(tmp_path, monkeypatch):
    monkeypatch.setattr(
        media,
        "probe",
        lambda path: {
            "duration": 5,
            "width": 640,
            "height": 360,
            "audio": False,
            "frame_rate": "30/1",
        },
    )
    monkeypatch.setattr(
        media,
        "process",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b""),
    )
    moments = iter([100.0, 103.0])
    monkeypatch.setattr(media.time, "time", lambda: next(moments))
    result = media.render(
        tmp_path / "input.mp4",
        tmp_path / "output.mp4",
        SimpleNamespace(start=0.0, end=5.0),
        Settings(aspect="16:9", height=360),
    )
    assert result["ffmpeg_process_timing"] == {
        "started_at": 100.0,
        "finished_at": 103.0,
    }
