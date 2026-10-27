"""Offline checks for window concurrency, GPU scheduling and private diagnostics."""

from contextlib import contextmanager
import importlib
import json
from pathlib import Path
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


@pytest.mark.parametrize("existing_is_symlink", [False, True])
def test_unverified_existing_overview_stops_before_any_request(
    tmp_path, monkeypatch, existing_is_symlink
):
    audio = tmp_path / "input.m4a"
    audio.write_bytes(b"synthetic input")
    output = tmp_path / "out"
    output.mkdir()
    stale = output / "overview-1.ogg"
    if existing_is_symlink:
        stale.symlink_to(tmp_path / "missing.ogg")
    else:
        stale.write_bytes(b"unrelated audio")
    monkeypatch.setattr(
        audio_understanding.audio_first,
        "audio_info",
        lambda _: {"duration": audio_understanding.WINDOW_SECONDS + 60},
    )
    monkeypatch.setattr(
        audio_understanding.Client,
        "generate",
        lambda *args, **kwargs: pytest.fail("no model request should be sent"),
    )
    monkeypatch.setattr(
        audio_understanding,
        "encode_audio",
        lambda *args, **kwargs: pytest.fail("no audio conversion should start"),
    )
    with pytest.raises(storage.PipelineError, match="Overview output already exists"):
        audio_understanding.plan(
            audio,
            "https://kick.com/example/videos/01a171d4-2920-7e7b-a842-e2b580698688",
            output,
            Settings(aspect="16:9", height=360),
        )
    assert not (output / "audio-editor-receipt.json").exists()


@pytest.mark.parametrize(
    "profile,height,config",
    [
        ([], 360, "config.landscape-long-preview.example.json"),
        (["--render-profile", "standard"], 720, "config.landscape-long.example.json"),
    ],
)
def test_new_production_defaults_to_360p_without_changing_explicit_profiles(
    tmp_path, monkeypatch, profile, height, config
):
    monkeypatch.syspath_prepend(
        str(__import__("pathlib").Path(__file__).parents[1] / "tools")
    )
    producer = importlib.import_module("produce_with_gemini")
    sent = []

    class StoppedBeforeDispatch(Exception):
        pass

    def fake_gh(*args, body=None):
        sent.append(json.loads(body))
        raise StoppedBeforeDispatch()

    monkeypatch.setattr(producer, "gh", fake_gh)
    monkeypatch.setattr(producer.os, "umask", lambda value: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "produce_with_gemini",
            "https://kick.com/example/videos/01a171d4-2920-7e7b-a842-e2b580698688",
            "--output",
            str(tmp_path / "new"),
            "--source-ref",
            "a" * 40,
            *profile,
        ],
    )
    with pytest.raises(StoppedBeforeDispatch):
        producer.main()
    state = json.loads((tmp_path / "new" / "production-state.json").read_text())
    assert state["render_height"] == height
    assert state["source_height_limit"] == 360
    assert state["config_file"] == config
    assert sent[0]["inputs"]["config_file"] == config


def test_resume_retains_existing_720p_setting_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(
        str(__import__("pathlib").Path(__file__).parents[1] / "tools")
    )
    producer = importlib.import_module("produce_with_gemini")
    output = tmp_path / "existing"
    output.mkdir()
    state = {
        "source_url": "https://kick.com/example/videos/01a171d4-2920-7e7b-a842-e2b580698688",
        "source_ref": "a" * 40,
        "repository": "nyu1791-collab/ClipForge",
        "config_file": "config.landscape-long.example.json",
        "render_height": 720,
        "target_clips": 1,
        "started_at": 0,
        "stages": [{"workflow": "clipforge-stages.yml", "request_id": "b" * 32}],
    }
    storage.write_json(output / "production-state.json", state)

    class StopMonitoring(Exception):
        pass

    def stop_monitoring(*args, **kwargs):
        raise StopMonitoring()

    monkeypatch.setattr(producer, "wait_for_stage", stop_monitoring)
    monkeypatch.setattr(
        producer, "gh", lambda *a, **k: pytest.fail("dispatch forbidden")
    )
    monkeypatch.setattr(producer.os, "umask", lambda value: 0)
    monkeypatch.setattr(
        sys, "argv", ["produce_with_gemini", "--resume", "--output", str(output)]
    )
    with pytest.raises(StopMonitoring):
        producer.main()
    saved = json.loads((output / "production-state.json").read_text())
    assert saved["config_file"] == "config.landscape-long.example.json"
    assert saved["render_height"] == 720
    assert len(saved["stages"]) == 1


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

    def prepare(self, completed):
        return {"identity": "test-" + str(next(iter(completed))), "clips": completed}

    def render(self, worker, prepared, state, lease):
        groups.append(tuple(prepared["clips"]))
        barrier.wait()

    monkeypatch.setattr(streaming_render, "gpu_slot", slot)
    monkeypatch.setattr(streaming_render.StreamingRenderer, "prepare", prepare)
    monkeypatch.setattr(streaming_render.StreamingRenderer, "render", render)
    renderer = streaming_render.StreamingRenderer(
        tmp_path, "source", [1, 2, 3], "config"
    )
    for index in range(1, 4):
        renderer.enqueue(index, {})
    renderer.finish()
    renderer.close()
    assert sorted(groups) == [(1,), (2,), (3,)]


def test_editorial_upload_finishes_before_waiting_for_a_gpu(tmp_path, monkeypatch):
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
    events = []
    waiting = threading.Event()
    allow_gpu = threading.Event()
    monkeypatch.setattr(
        streaming_render, "private_repo", lambda: events.append("private")
    )
    monkeypatch.setattr(streaming_render, "release", lambda tag: None)
    monkeypatch.setattr(
        streaming_render, "gh", lambda *args, **kwargs: events.append("published")
    )
    monkeypatch.setattr(
        streaming_render.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
    )

    class Lease:
        slot_index = 0

        def mark_requested(self):
            events.append("requested")

        def confirm_release(self):
            events.append("released")

    @contextmanager
    def slot(kind, control_state):
        events.append("waiting")
        waiting.set()
        assert allow_gpu.wait(5)
        yield Lease()

    monkeypatch.setattr(streaming_render, "gpu_slot", slot)
    clip = SimpleNamespace(model_dump=lambda: {"start": 0, "end": 10})
    renderer = streaming_render.StreamingRenderer(tmp_path, "source", [clip], "config")
    renderer.enqueue(
        1,
        {
            "turns": [],
            "sound_effects": [],
            "transcript": {"language": "ja", "segments": []},
        },
    )
    try:
        assert waiting.wait(5)
        assert events[:4] == ["private", "published", "published", "waiting"]
    finally:
        allow_gpu.set()
        renderer.close()
    assert events[-2:] == ["requested", "released"]
    assert list(renderer.state["groups"].values())[0]["status"] == "complete"


@pytest.mark.parametrize(
    "failure,expected_status,expected_phase",
    [
        ("publish", "publishing_or_unknown", None),
        ("wait", "ready_for_gpu", "waiting_for_gpu"),
        ("stop", "gpu_requested_or_unknown", "running"),
    ],
)
def test_render_failure_preserves_request_and_gpu_state(
    tmp_path, monkeypatch, failure, expected_status, expected_phase
):
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
    monkeypatch.setattr(streaming_render, "private_repo", lambda: None)
    monkeypatch.setattr(streaming_render, "release", lambda tag: None)
    events = []

    def publish(*args, **kwargs):
        if failure == "publish":
            raise RuntimeError("synthetic uncertain publication")

    monkeypatch.setattr(streaming_render, "gh", publish)

    class Lease:
        slot_index = 0

        def mark_requested(self):
            events.append("requested")

        def confirm_release(self):
            events.append("confirmed")

    @contextmanager
    def slot(kind, control_state):
        events.append("slot")
        if failure == "wait":
            raise RuntimeError("synthetic slot timeout")
        yield Lease()

    def control(args, **kwargs):
        action = args[2]
        if action == "start":
            state_dir = Path(args[args.index("--state-dir") + 1])
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "state.json").write_text('{"status":"allocated"}')
        return subprocess.CompletedProcess(args, 1 if action == "stop" else 0)

    monkeypatch.setattr(streaming_render, "gpu_slot", slot)
    monkeypatch.setattr(streaming_render.subprocess, "run", control)
    clip = SimpleNamespace(model_dump=lambda: {"start": 0, "end": 10})
    renderer = streaming_render.StreamingRenderer(tmp_path, "source", [clip], "config")
    renderer.enqueue(
        1,
        {
            "turns": [],
            "sound_effects": [],
            "transcript": {"language": "ja", "segments": []},
        },
    )
    with pytest.raises(storage.PipelineError, match="render batches failed"):
        renderer.close()
    group = list(renderer.state["groups"].values())[0]
    assert group["status"] == expected_status
    assert group.get("failed_phase") == expected_phase
    if failure == "publish":
        assert "slot" not in events
    if failure == "stop":
        assert "requested" in events
        assert "confirmed" not in events


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
                    "text": "CLIPFORGE_DIAG_STAGE_render_RuntimeError\n"
                    "CLIPFORGE_DIAG_INIT_ASR\nCLIPFORGE_DIAG_WORKER_ERROR_ValueError",
                }
            )
            return []

    control.checked_execute(Runtime(), "synthetic", 1)
    printed = capsys.readouterr().out
    assert "CLIPFORGE_BOOTSTRAP_WORKER" in printed
    assert "CLIPFORGE_DIAG_STAGE_render_RuntimeError" in printed
    assert "CLIPFORGE_DIAG_INIT_ASR" in printed
    assert "CLIPFORGE_DIAG_WORKER_ERROR_ValueError" in printed
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
    timing = {}
    with media.capture_ffmpeg_timing(timing):
        result = media.render(
            tmp_path / "input.mp4",
            tmp_path / "output.mp4",
            SimpleNamespace(start=0.0, end=5.0),
            Settings(aspect="16:9", height=360),
        )
    assert timing == {"ffmpeg_started_at": 100.0, "ffmpeg_finished_at": 103.0}
    assert "ffmpeg_process_timing" not in result
