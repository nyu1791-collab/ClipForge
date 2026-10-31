from pathlib import Path
import wave

import pytest
import yaml

from clipforge import asr_split, audio_first
from clipforge.models import Settings
from clipforge.storage import digest, write_json
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
        "Recognise the audio parts on owned Colab GPUs and allocate topics",
    ]
    for step in steps:
        if step.get("name") in holders:
            assert step["env"]["COLAB_ADC_JSON"] == "${{ secrets.COLAB_ADC_JSON }}"
            assert "GEMINI_API_KEY" not in step["env"]


def test_release_step_follows_recognition_and_needs_no_credential_secret():
    _, steps = colab_steps()
    names = [step.get("name") for step in steps]
    recognise = names.index(
        "Recognise the audio parts on owned Colab GPUs and allocate topics"
    )
    release = names.index("Release and verify removal of every owned Colab runtime")
    assert recognise < release
    # colab_actions.py stop reads adc.json saved in each state directory and never
    # reads COLAB_ADC_JSON, so the release step does not receive the Secret.
    env = steps[release].get("env", {})
    assert "COLAB_ADC_JSON" not in env and "GEMINI_API_KEY" not in env
    assert steps[release]["if"] == "always() && steps.control.outcome == 'success'"
    assert "clipforge-split/control-*" in steps[release]["run"]


@pytest.fixture
def two_part_plan(tmp_path):
    source = tmp_path / "audio.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * 16000 * 40)
    settings = Settings(asr_parts=2, min_seconds=3, max_seconds=10)
    request = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "video_sha256": digest(source),
        "video_bytes": source.stat().st_size,
        "settings": settings.model_dump(),
        "audio_source_url": URL,
    }
    plan = asr_split.prepare(
        source, tmp_path / "parts", request, settings, audio_first.audio_info(source)
    )
    directory = tmp_path / "parent"
    directory.mkdir()
    (directory / "input-video.bin").write_bytes(source.read_bytes())
    write_json(directory / "request.json", plan["request"])
    return directory, plan, tmp_path


def test_the_two_longest_parts_try_l4_first_and_every_part_accepts_any_gpu():
    plan = {
        "parts": [
            {"asr_part": {"start": 0, "end": value}}
            for value in (3000, 4000, 2500, 3500)
        ]
    }
    assert split_asr_job.pools(plan) == [
        split_asr_job.POOL,
        split_asr_job.FAST_POOL,
        split_asr_job.POOL,
        split_asr_job.FAST_POOL,
    ]
    assert split_asr_job.FAST_POOL.startswith("L4,")
    assert split_asr_job.POOL.startswith("T4:")
    assert (
        {"L4", "T4:high-ram", "T4:standard"}
        == set(split_asr_job.FAST_POOL.split(","))
        == set(split_asr_job.POOL.split(","))
    )
