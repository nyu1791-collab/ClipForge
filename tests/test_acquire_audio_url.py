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
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
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

    def invoke(url=URL, request_id="b" * 32, source_ref="a" * 40, settings_json=None):
        module.main(
            [
                "--url",
                url,
                "--request-id",
                request_id,
                "--source-ref",
                source_ref,
                "--settings-json",
                json.dumps(request["settings"])
                if settings_json is None
                else settings_json,
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
    "settings",
    [
        {"unexpected": True},
        {"bgm": "synthetic.mp3"},
        {"gemini_model": "test-model"},
        {"gemini_model": "manual-no-api", "image_search": True},
        {"gemini_model": "manual-no-api", "table_query": "synthetic"},
        {"gemini_model": "manual-no-api", "chat_url": "https://example.invalid"},
    ],
)
def test_acquisition_rejects_unknown_local_or_enriched_settings(acquisition, settings):
    request, output, control, invoke = acquisition
    request["settings"] = settings
    with pytest.raises(RuntimeError):
        invoke()
    assert not control["created"] and not output.exists()


@pytest.mark.parametrize("settings_json", ["{", "[]", "null", " " * 16385])
def test_acquisition_rejects_invalid_or_oversized_settings_before_network(
    acquisition, settings_json
):
    _, output, control, invoke = acquisition
    with pytest.raises((RuntimeError, json.JSONDecodeError)):
        invoke(settings_json=settings_json)
    assert not control["created"] and not output.exists()


@pytest.mark.parametrize(
    "identity", [{"request_id": "invalid"}, {"source_ref": "invalid"}]
)
def test_acquisition_rejects_invalid_identity_before_network(acquisition, identity):
    _, output, control, invoke = acquisition
    with pytest.raises(RuntimeError, match="identity"):
        invoke(**identity)
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
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["settings_json"]["type"] == "string"
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
    assert steps[acquire]["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert steps[acquire]["env"]["SETTINGS_JSON"] == "${{ inputs.settings_json }}"
    script = steps[acquire]["run"]
    assert script.index("gh release create") < script.index(
        "tools/acquire_audio_url.py"
    )
    assert script.index("tools/acquire_audio_url.py") < script.index(
        "gh release upload"
    )
    assert '--settings-json "$SETTINGS_JSON"' in script
    assert "gh release download" not in script
    assert '"$RUNNER_TEMP/clipforge-input/input-video.bin"' in steps[acquire]["run"]
    assert '"$RUNNER_TEMP/clipforge-input/request.json"' in steps[acquire]["run"]
    validate = steps[0]["run"]
    assert validate.index("if os.environ['AUDIO_URL']") < validate.index(
        "if not os.environ.get('COLAB_ADC_JSON')"
    )
    assert all("AUDIO_URL" not in step.get("env", {}) for step in steps[allocate:])
    assert "AUDIO_URL" not in job["env"]
    cleanup = steps[-1]
    assert cleanup["if"] == "always()"
    assert 'cleanup --tag "$INPUT_TAG"' in cleanup["run"]
    assert all("tools/audio_url_job.py" not in step.get("run", "") for step in steps)
    assert all("apt-get" not in step.get("run", "") for step in steps)
    assert all("ffmpeg" not in json.dumps(step).lower() for step in steps)
    assert all(".deb" not in json.dumps(step) for step in steps)
    assert steps[sync]["timeout-minutes"] == 10
    assert steps[acquire]["timeout-minutes"] == 30
    request = next(i for i, step in enumerate(steps) if step.get("id") == "request")
    assert acquire < request < allocate
    run = next(step for step in steps if "colab_actions.py run" in step.get("run", ""))
    assert (
        run["env"]["CLIPFORGE_NO_MODEL_API"]
        == "${{ steps.request.outputs.no_model_api }}"
    )
    assert run["env"]["GEMINI_API_KEY"] == (
        "${{ steps.request.outputs.no_model_api == '0' && secrets.GEMINI_API_KEY || '' }}"
    )


def test_workflow_steps_have_timeouts_and_colab_has_execution_and_cleanup_time():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert 0 < step["timeout-minutes"] <= job["timeout-minutes"]
    job = workflow["jobs"]["colab-auto"]
    run = next(
        step for step in job["steps"] if "colab_actions.py run" in step.get("run", "")
    )
    assert run["timeout-minutes"] == 90
    assert job["timeout-minutes"] >= 30 + run["timeout-minutes"] + 20


def test_cpu_render_package_install_retains_media_tools_with_timeout():
    workflow = yaml.safe_load((ROOT / "deploy/audio-stages.yml").read_text())
    job = workflow["jobs"]["audio-stage"]
    step = next(s for s in job["steps"] if "apt-get update" in s.get("run", ""))
    assert step["timeout-minutes"] == 10
    assert "ffmpeg" in job["env"]["CPU_PACKAGES"]
    assert "fonts-noto-cjk" in job["env"]["CPU_PACKAGES"]


@pytest.mark.parametrize(
    "audio_url, local_audio", [(None, True), (URL, False), (URL, True)]
)
def test_workflow_detects_model_requirements_from_request(
    tmp_path, monkeypatch, audio_url, local_audio
):
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["colab-auto"]["steps"]
        if step.get("id") == "request"
    )
    directory = tmp_path / "clipforge-model-request"
    directory.mkdir()
    request = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "video_sha256": "c" * 64 if local_audio else None,
    }
    if audio_url:
        request["audio_source_url"] = audio_url
    (directory / "request.json").write_text(json.dumps(request))
    output = tmp_path / "output"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("REQUEST_ID", request["request_id"])
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", request["source_ref"])
    code = step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    exec(compile(code, "workflow-request", "exec"), {})
    assert output.read_text() == f"no_model_api={int(bool(audio_url))}\n"


def test_maintenance_workflow_passes_cache_profile_to_allocation():
    workflow = yaml.safe_load((ROOT / "deploy/colab-maintenance.yml").read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["cache_profile"] == {
        "description": "Public asset cache profile for prepare-cache",
        "type": "choice",
        "options": ["gpu", "cpu"],
        "default": "gpu",
    }
    allocate = next(
        step
        for step in workflow["jobs"]["colab"]["steps"]
        if "colab_actions.py start" in step.get("run", "")
    )
    assert allocate["env"]["CLIPFORGE_CACHE_PROFILE"] == "${{ inputs.cache_profile }}"
