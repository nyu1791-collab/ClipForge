import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
URL = "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001"


@pytest.fixture
def acquisition(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "test_acquire_audio_url", ROOT / "tools/acquire_audio_url.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    request = {
        "request_id": "b" * 32,
        "source_ref": "a" * 40,
        "video_sha256": None,
        "video_bytes": None,
        "settings": {
            "gemini_model": "manual-no-api",
            "max_input_bytes": 8192,
            "max_source_seconds": 600,
            "asr_batch_size": 8,
        },
        "chat_sha256": None,
        "audio_source_url": URL,
    }
    request_path = tmp_path / "request.json"
    output = tmp_path / "input"
    control = {
        "created": False,
        "closed": False,
        "fail": False,
        "audio": b"synthetic-audio",
    }

    class VOD:
        def __init__(self, url, cache, **kwargs):
            assert url == URL
            assert kwargs == {"max_bytes": 8192, "max_seconds": 600}
            self.cache = cache
            control["created"] = True

        def close(self):
            control["closed"] = True

    def acquire(vod, root):
        assert vod.cache == root / "segments"
        if control["fail"]:
            raise RuntimeError("synthetic acquisition failure")
        (root / "audio.m4a").write_bytes(control["audio"])
        (root / "acquisition.json").write_text("{}")

    monkeypatch.setattr(module, "KickVOD", VOD)
    monkeypatch.setattr(module, "acquire", acquire)

    def invoke(url=URL):
        request_path.write_text(json.dumps(request))
        module.main(
            [
                "--url",
                url,
                "--request-id",
                "b" * 32,
                "--source-ref",
                "a" * 40,
                "--request",
                str(request_path),
                "--directory",
                str(output),
            ]
        )

    return request, output, control, invoke


def test_acquisition_writes_prepare_input_and_preserves_settings(acquisition, capsys):
    request, output, control, invoke = acquisition
    invoke()
    assert {path.name for path in output.iterdir()} == {
        "input-video.bin",
        "request.json",
    }
    audio = b"synthetic-audio"
    assert (output / "input-video.bin").read_bytes() == audio
    assert json.loads((output / "request.json").read_text()) == {
        **request,
        "video_sha256": hashlib.sha256(audio).hexdigest(),
        "video_bytes": len(audio),
    }
    assert control["closed"]
    assert not list(output.parent.glob(".clipforge-acquire-*"))
    assert URL not in capsys.readouterr().out


@pytest.mark.parametrize(
    "patch",
    [
        {"request_id": "c" * 32},
        {"source_ref": "c" * 40},
        {"audio_source_url": URL + "?extra=1"},
        {"video_sha256": "e" * 64},
        {"video_bytes": 1},
        {"chat_sha256": "e" * 64},
        {"unexpected": True},
        {"settings": {"gemini_model": "test-model"}},
        {"settings": {"gemini_model": "manual-no-api", "image_search": True}},
    ],
)
def test_acquisition_rejects_mismatched_or_enriched_requests(acquisition, patch):
    request, output, control, invoke = acquisition
    request.update(patch)
    with pytest.raises(RuntimeError):
        invoke()
    assert not control["created"] and not output.exists()


def test_acquisition_validates_url_before_network(acquisition):
    from clipforge.storage import PipelineError

    _, _, control, invoke = acquisition
    with pytest.raises(PipelineError, match="canonical"):
        invoke(URL + "?extra=1")
    assert not control["created"]


def test_acquisition_closes_vod_and_removes_temporary_media_on_failure(acquisition):
    _, output, control, invoke = acquisition
    control["fail"] = True
    with pytest.raises(RuntimeError, match="synthetic acquisition"):
        invoke()
    assert control["closed"] and not list(output.iterdir())
    assert not list(output.parent.glob(".clipforge-acquire-*"))


@pytest.mark.parametrize("audio", [b"", b"x" * 8193])
def test_acquisition_refuses_empty_or_oversized_audio(acquisition, audio):
    _, output, control, invoke = acquisition
    control["audio"] = audio
    with pytest.raises(RuntimeError, match="size limit"):
        invoke()
    assert control["closed"] and not list(output.iterdir())
    assert not list(output.parent.glob(".clipforge-acquire-*"))


def test_workflow_acquires_and_uploads_audio_before_allocating_gpu():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    job = workflow["jobs"]["colab-auto"]
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    allocate = next(i for i, step in enumerate(steps) if step.get("id") == "allocate")
    acquire = next(
        i
        for i, step in enumerate(steps)
        if "tools/acquire_audio_url.py" in step.get("run", "")
    )
    sync = next(
        i
        for i, step in enumerate(steps)
        if step.get("run") == "uv sync --locked --extra media-fetch --no-dev"
    )
    assert sync < acquire < allocate
    assert steps[sync]["working-directory"] == "clipforge-source"
    assert steps[acquire]["if"] == "inputs.audio_url != ''"
    assert '"$RUNNER_TEMP/clipforge-input/input-video.bin"' in steps[acquire]["run"]
    assert '"$RUNNER_TEMP/clipforge-input/request.json"' in steps[acquire]["run"]
    validate = steps[0]["run"]
    assert validate.index("if os.environ['AUDIO_URL']") < validate.index(
        "if not os.environ.get('COLAB_ADC_JSON')"
    )
    assert all("AUDIO_URL" not in step.get("env", {}) for step in steps[allocate:])
    assert "AUDIO_URL" not in job["env"]
    assert all("tools/audio_url_job.py" not in step.get("run", "") for step in steps)
    cache = next(step for step in steps if step.get("uses") == "actions/cache@v4")
    assert cache["if"] == "inputs.audio_url != ''"
    assert cache["with"]["path"].endswith("/clipforge-ffmpeg-debs/*.deb")
    install = next(
        step["run"] for step in steps if "--no-download" in step.get("run", "")
    )
    cached, uncached = install.split("\nelse\n")
    assert "command -v ffmpeg" in cached and "command -v ffprobe" in cached
    assert "--no-download" in cached and "apt-get update" not in cached
    assert "apt-get update" in uncached
