import subprocess
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
import yaml

from clipforge import asr_split, audio_first
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json
from tools import split_asr_job

ROOT = Path(__file__).resolve().parents[1]
URL = "https://cdn.example/audio.mp3?signature=private-test"
CREDENTIAL = '{"type": "authorized_user", "synthetic": true}'


def colab_steps():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    job = workflow["jobs"]["colab-auto"]
    return job, job["steps"]


def test_colab_credential_is_scoped_to_steps_that_start_or_split_runtimes():
    job, steps = colab_steps()
    assert "COLAB_ADC_JSON" not in job["env"]
    holders = [
        step["name"] for step in steps if "COLAB_ADC_JSON" in step.get("env", {})
    ]
    assert holders == [
        "Validate request and one-time Colab OAuth setup",
        "Allocate a dedicated T4 for the verified Google account",
        "Run owned ASR runtimes in parallel and merge before topic allocation",
    ]
    for step in steps:
        if step.get("name") in holders:
            assert step["env"]["COLAB_ADC_JSON"] == "${{ secrets.COLAB_ADC_JSON }}"
            assert "GEMINI_API_KEY" not in step["env"]


def test_two_part_run_gets_credential_without_model_key():
    _, steps = colab_steps()
    split = next(
        step
        for step in steps
        if step.get("if") == "steps.request.outputs.asr_parts != '1'"
    )
    assert "split_asr_job.py" in split["run"]
    assert split["env"]["COLAB_ADC_JSON"] == "${{ secrets.COLAB_ADC_JSON }}"
    assert "GEMINI_API_KEY" not in split["env"]


def test_two_part_release_step_follows_split_and_needs_no_credential_secret():
    _, steps = colab_steps()
    names = [step.get("name") for step in steps]
    split = names.index(
        "Run owned ASR runtimes in parallel and merge before topic allocation"
    )
    release = names.index("Release and verify removal of the dedicated Colab runtime")
    assert split < release
    # colab_actions.py stop reads adc.json saved in each state directory and never
    # reads COLAB_ADC_JSON, so the release step (shared with the single-part path)
    # does not receive the Secret.
    env = steps[release].get("env", {})
    assert "COLAB_ADC_JSON" not in env and "GEMINI_API_KEY" not in env
    assert steps[release]["if"] == "always() && steps.control.outcome == 'success'"


@pytest.fixture
def two_part_plan(tmp_path):
    source = tmp_path / "audio.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * 16000 * 40)
    settings = Settings(
        gemini_model="manual-no-api",
        asr_parts=2,
        whisper_device="cuda",
        whisper_compute_type="float16",
        min_seconds=3,
        max_seconds=10,
    )
    request = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "video_sha256": digest(source),
        "video_bytes": source.stat().st_size,
        "settings": settings.model_dump(),
        "audio_source_url": URL,
        "chat_sha256": None,
    }
    plan = asr_split.prepare(
        source, tmp_path / "parts", request, settings, audio_first.audio_info(source)
    )
    directory = tmp_path / "parent"
    directory.mkdir()
    (directory / "input-video.bin").write_bytes(source.read_bytes())
    write_json(directory / "request.json", plan["request"])
    return directory, plan, tmp_path


def test_split_children_receive_colab_credential_and_never_the_model_key(
    two_part_plan, monkeypatch
):
    directory, plan, root = two_part_plan
    monkeypatch.setenv("COLAB_ADC_JSON", CREDENTIAL)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-reach-colab")
    monkeypatch.setenv("GITHUB_REPOSITORY", split_asr_job.PRIVATE_REPO)
    monkeypatch.setenv("REQUEST_ID", plan["request"]["request_id"])
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", plan["request"]["source_ref"])
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setattr(
        split_asr_job.github_cli,
        "run",
        lambda *arguments, **kwargs: "true" if arguments[0] == "api" else "",
    )
    calls = []

    def colab_actions(argv, **kwargs):
        # argv: python tools/colab_actions.py <command> --state-dir S --output O
        command, state = argv[2], Path(argv[4])
        calls.append(
            {
                "command": command,
                "state": state.name,
                "environment": kwargs["env"],
            }
        )
        if command == "start":
            write_json(state / "state.json", {"status": "allocated"})
            return subprocess.CompletedProcess(argv, 0)
        # Fail the run so no ASR outputs are needed; the coordinator must still stop.
        return subprocess.CompletedProcess(argv, 1 if command == "run" else 0)

    monkeypatch.setattr(
        split_asr_job,
        "subprocess",
        SimpleNamespace(run=colab_actions, TimeoutExpired=subprocess.TimeoutExpired),
    )
    args = SimpleNamespace(
        directory=directory,
        output=root / "combined",
        control_python="python",
        state_root=root / "coordinator",
    )
    with pytest.raises(PipelineError, match="split ASR failed"):
        split_asr_job.run(args)

    assert sorted(call["state"] for call in calls if call["command"] == "start") == [
        "control-0",
        "control-1",
    ]
    for call in calls:
        assert call["environment"]["COLAB_ADC_JSON"] == CREDENTIAL
        assert "GEMINI_API_KEY" not in call["environment"]
    assert sorted(
        call["command"] for call in calls if call["state"] == "control-0"
    ) == [
        "run",
        "start",
        "stop",
    ]
