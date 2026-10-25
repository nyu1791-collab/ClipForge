import hashlib
import os
import subprocess
import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
URL = "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001"
AUDIO_URL = "https://cdn.example/audio.mp3"


def test_url_probe_does_not_allocate_gpu_or_pass_colab_credentials():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    inputs = workflow.get(True, workflow.get("on"))["workflow_dispatch"]["inputs"]
    assert len(inputs) == 10 and inputs["probe_only"]["default"] is False
    assert inputs["result_store"]["type"] == "choice"
    assert "!inputs.probe_only" in workflow["jobs"]["colab-auto"]["if"]
    probe = workflow["jobs"]["url-probe"]
    assert "inputs.probe_only" in probe["if"]
    assert "secrets." not in json.dumps(probe)
    assert "colab_actions.py" not in json.dumps(probe)
    assert "tools/probe_youtube_pot.py" in json.dumps(probe)
    diagnostic = (ROOT / "tools/probe_youtube_pot.py").read_text()
    assert '"skip_download": True' in diagnostic
    assert 'os.environ.pop("YOUTUBE_COOKIES_B64", None)' in diagnostic
    assert "time.monotonic()" in diagnostic


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
            "max_input_bytes": 8192,
            "max_transport_bytes": 32768,
            "max_source_seconds": 600,
            "asr_parts": 2,
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
            assert kwargs == {"max_bytes": 32768, "max_seconds": 600}
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

    def invoke(
        url=URL, request_id="b" * 32, source_ref="a" * 40, settings_json=None, extra=()
    ):
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
                *extra,
            ]
        )

    return request, output, control, invoke


def test_acquisition_writes_prepare_input_and_preserves_settings(acquisition, capsys):
    request, output, control, invoke = acquisition
    invoke()
    assert {path.name for path in output.iterdir()} == {
        "input-video.bin",
        "request.json",
        "acquisition-report.json",
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
        {"image_search": True},
        {"table_query": "synthetic"},
        {"chat_url": "https://example.invalid"},
    ],
)
def test_acquisition_rejects_unknown_local_or_enriched_settings(acquisition, settings):
    request, output, control, invoke = acquisition
    request["settings"] = settings
    with pytest.raises(RuntimeError):
        invoke()
    assert not control["created"]
    refused(output, "invalid_settings")


@pytest.mark.parametrize("settings_json", ["{", "[]", "null", " " * 16385])
def test_acquisition_rejects_invalid_or_oversized_settings_before_network(
    acquisition, settings_json
):
    _, output, control, invoke = acquisition
    with pytest.raises((RuntimeError, json.JSONDecodeError)):
        invoke(settings_json=settings_json)
    assert not control["created"]
    refused(output, "invalid_settings")


@pytest.mark.parametrize(
    "identity", [{"request_id": "invalid"}, {"source_ref": "invalid"}]
)
def test_acquisition_rejects_invalid_identity_before_network(acquisition, identity):
    _, output, control, invoke = acquisition
    with pytest.raises(RuntimeError, match="identity"):
        invoke(**identity)
    assert not control["created"]
    refused(output, "request_mismatch")


def test_acquisition_validates_url_before_network(acquisition):
    from clipforge.storage import PipelineError

    _, output, control, invoke = acquisition
    with pytest.raises(PipelineError, match="canonical"):
        invoke(URL + "?extra=1")
    assert not control["created"]
    refused(output, "invalid_url")


def refused(output, reason):
    assert {path.name for path in output.iterdir()} == {"acquisition-report.json"}
    text = (output / "acquisition-report.json").read_text()
    report = json.loads(text)
    assert report["status"] == "acquisition_failed"
    assert report["phase"] == "input_validation"
    assert report["reason"] == reason
    assert report["gpu_allocated"] is False and report["asr_started"] is False
    # Identity is recorded only once it has been checked, so earlier refusals omit it.
    if reason == "invalid_settings":
        assert report["request_id"] == "b" * 32 and report["source_ref"] == "a" * 40
    else:
        assert "request_id" not in report and "source_ref" not in report
    assert "https://" not in text
    return report


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"extra": ["--require-video"], "url": AUDIO_URL}, "audio_only_source"),
        ({"extra": ["--verified-audio-dir", "/nonexistent"]}, "invalid_reuse"),
    ],
)
def test_refused_inputs_leave_a_reason_code_and_never_reach_the_network(
    acquisition, kwargs, reason
):
    from clipforge.storage import PipelineError

    _, output, control, invoke = acquisition
    with pytest.raises(PipelineError):
        invoke(**kwargs)
    assert not control["created"]
    refused(output, reason)


def test_refused_reused_artifact_records_its_own_reason_code(
    acquisition, monkeypatch, tmp_path
):
    from clipforge import audio_inputs
    from clipforge.storage import PipelineError

    def refuse(*args, **kwargs):
        raise PipelineError("synthetic artifact mismatch")

    monkeypatch.setattr(audio_inputs, "verify_audio_artifact", refuse)
    _, output, control, invoke = acquisition
    producer = tmp_path / "producer"
    producer.mkdir()
    with pytest.raises(PipelineError, match="synthetic artifact mismatch"):
        invoke(
            extra=[
                "--verified-audio-dir",
                str(producer),
                "--verified-audio-run",
                "7",
                "--verified-audio-request",
                "c" * 32,
                "--verified-audio-source-ref",
                "d" * 40,
            ]
        )
    assert not control["created"]
    assert {path.name for path in output.iterdir()} == {"acquisition-report.json"}
    report = json.loads((output / "acquisition-report.json").read_text())
    assert report["reason"] == "invalid_reuse_artifact"
    assert report["phase"] == "reuse_verify"
    assert report["status"] == "acquisition_failed"
    assert report["gpu_allocated"] is False and report["asr_started"] is False
    assert "https://" not in json.dumps(report)


def test_successful_acquisition_report_keeps_identity_and_transport(acquisition):
    _, output, _, invoke = acquisition
    invoke()
    report = json.loads((output / "acquisition-report.json").read_text())
    assert report["status"] == "audio_acquired" and report["phase"] == "complete"
    assert report["request_id"] == "b" * 32 and report["source_ref"] == "a" * 40
    assert report["transport"] == "standard" and "reason" not in report


def test_acquisition_closes_vod_and_removes_temporary_media_on_failure(acquisition):
    _, output, control, invoke = acquisition
    control["fail"] = True
    with pytest.raises(RuntimeError, match="synthetic acquisition"):
        invoke()
    assert control["closed"]
    assert {p.name for p in output.iterdir()} == {"acquisition-report.json"}
    assert (
        json.loads((output / "acquisition-report.json").read_text())["status"]
        == "acquisition_failed"
    )
    assert not list(output.parent.glob(".clipforge-acquire-*"))


@pytest.mark.parametrize("audio", [b"", b"x" * 8193])
def test_acquisition_refuses_empty_or_oversized_audio(acquisition, audio):
    _, output, control, invoke = acquisition
    control["audio"] = audio
    with pytest.raises(RuntimeError, match="size limit"):
        invoke()
    assert control["closed"]
    assert {p.name for p in output.iterdir()} == {"acquisition-report.json"}
    assert (
        json.loads((output / "acquisition-report.json").read_text())["status"]
        == "acquisition_failed"
    )
    assert not list(output.parent.glob(".clipforge-acquire-*"))


def test_workflow_acquires_and_verifies_audio_before_any_gpu():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    inputs = workflow.get(True, workflow.get("on"))["workflow_dispatch"]["inputs"]
    assert inputs["settings_json"]["type"] == "string"
    job = workflow["jobs"]["colab-auto"]
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    acquire = next(
        i
        for i, step in enumerate(steps)
        if "tools/acquire_audio_url.py" in step.get("run", "")
    )
    recognise = next(
        i
        for i, step in enumerate(steps)
        if "tools/split_asr_job.py" in step.get("run", "")
    )
    sync = next(
        i
        for i, step in enumerate(steps)
        if step.get("run") == "uv sync --locked --extra media-fetch --no-dev"
    )
    media_tools = next(
        i for i, step in enumerate(steps) if "command -v ffmpeg" in step.get("run", "")
    )
    assert sync < media_tools < acquire < recognise
    assert steps[sync]["working-directory"] == "clipforge-source"
    assert steps[acquire]["if"] == "inputs.audio_url != ''"
    assert "GH_TOKEN" not in steps[acquire]["env"]
    assert steps[acquire]["env"]["SETTINGS_JSON"] == "${{ inputs.settings_json }}"
    script = steps[acquire]["run"]
    assert '--settings-json "$SETTINGS_JSON"' in script
    # The coordinator reads the acquired audio on the runner; it is never staged whole.
    assert "gh release" not in script
    assert '--directory "$RUNNER_TEMP/clipforge-input"' in steps[recognise]["run"]
    validate = next(
        step["run"] for step in steps if "COLAB_ADC_JSON" in step.get("env", {})
    )
    assert validate.index("if os.environ['AUDIO_URL']") < validate.index(
        "if not os.environ.get('COLAB_ADC_JSON')"
    )
    assert all("AUDIO_URL" not in step.get("env", {}) for step in steps[recognise:])
    assert "AUDIO_URL" not in job["env"]
    cleanup = next(step for step in steps if step.get("id") == "input-cleanup")
    assert cleanup["if"] == "always()"
    assert 'gh release delete "$INPUT_TAG"' in cleanup["run"]
    assert steps[media_tools]["if"] == "inputs.audio_url != ''"
    assert "GH_TOKEN" not in steps[media_tools].get("env", {})
    assert steps[sync]["timeout-minutes"] == 10
    assert steps[acquire]["timeout-minutes"] == 30
    assert "GEMINI_API_KEY" not in steps[recognise]["env"]


def test_workflow_steps_have_timeouts_and_recognition_has_cleanup_time():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert 0 < step["timeout-minutes"] <= job["timeout-minutes"]
    job = workflow["jobs"]["colab-auto"]
    recognise = next(
        step for step in job["steps"] if "split_asr_job.py" in step.get("run", "")
    )
    assert recognise["timeout-minutes"] == 140
    assert job["timeout-minutes"] >= 30 + recognise["timeout-minutes"] + 10


def test_cpu_render_package_install_retains_media_tools_with_timeout():
    workflow = yaml.safe_load((ROOT / "deploy/audio-stages.yml").read_text())
    job = workflow["jobs"]["audio-stage"]
    step = next(s for s in job["steps"] if "apt-get update" in s.get("run", ""))
    assert step["timeout-minutes"] == 10
    assert "ffmpeg" in job["env"]["CPU_PACKAGES"]
    assert "fonts-noto-cjk" in job["env"]["CPU_PACKAGES"]


def test_chained_previews_workflow_dependencies_identity_artifacts_and_packages():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    inputs = workflow.get(True, workflow.get("on"))["workflow_dispatch"]["inputs"]
    assert inputs["auto_previews"] == {
        "description": "Continue URL-only analysis with CPU previews and an unapproved review draft",
        "type": "boolean",
        "default": False,
    }
    assert workflow["permissions"]["actions"] == "read"
    analysis = workflow["jobs"]["colab-auto"]["steps"]
    upload = next(
        s
        for s in analysis
        if s.get("uses") == "actions/upload-artifact@v4"
        and s["with"]["name"].startswith("clipforge-${{ inputs.auto_previews")
    )
    assert (
        upload["with"]["name"]
        == "clipforge-${{ inputs.auto_previews && 'analysis' || 'result' }}-${{ inputs.request_id }}"
    )
    stop = next(
        i for i, s in enumerate(analysis) if "colab_actions.py stop" in s.get("run", "")
    )
    assert stop < analysis.index(upload)
    job = workflow["jobs"]["previews"]
    assert job["needs"] == "colab-auto"
    assert "inputs.auto_previews" in job["if"]
    assert "needs.colab-auto.result == 'success'" in job["if"]
    assert "github.event.repository.private" in job["if"]
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    checkout = next(s for s in steps if s.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["ref"] == "${{ inputs.source_ref }}"
    download = next(s for s in steps if s.get("uses") == "actions/download-artifact@v4")
    assert download["with"]["name"] == "clipforge-analysis-${{ inputs.request_id }}"
    assert "run-id" not in download["with"]
    execute = next(s for s in steps if "tools/run_audio_stage.py" in s.get("run", ""))
    assert '--same-run-analysis "$RUNNER_TEMP/clipforge-analysis"' in execute["run"]
    assert (
        '--previous-run "$GITHUB_RUN_ID" --previous-request "$REQUEST_ID"'
        in execute["run"]
    )
    assert '--config "$RUNNER_TEMP/preview-settings.json"' in execute["run"]
    upload = next(
        step
        for step in steps
        if step.get("uses") == "actions/upload-artifact@v4"
        and step["with"]["name"] == "clipforge-result-${{ inputs.request_id }}"
    )
    assert upload["with"]["name"] == "clipforge-result-${{ inputs.request_id }}"
    assert upload["if"] == "always() && inputs.result_store != 'release'"
    transfer = next(
        step for step in steps if step.get("name") == "Save the verified private result transfer"
    )
    assert "inputs.result_store == 'release'" in transfer["if"]
    cpu = yaml.safe_load((ROOT / "deploy/audio-stages.yml").read_text())["jobs"][
        "audio-stage"
    ]
    assert job["env"]["CPU_PACKAGES"] == cpu["env"]["CPU_PACKAGES"]
    for name in (
        "Prepare CPU package cache",
        "Restore CPU package archives",
        "Install missing CPU media tools and fonts",
    ):
        step = next(s for s in steps if s.get("name") == name)
        original = next(s for s in cpu["steps"] if s.get("name") == name)
        for key in ("run", "uses", "with"):
            assert step.get(key) == original.get(key)


def test_chained_previews_preserve_submitted_settings(tmp_path, monkeypatch):
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    step = next(
        s
        for s in workflow["jobs"]["previews"]["steps"]
        if s.get("name") == "Prepare the submitted preview settings"
    )
    settings = json.loads((ROOT / "config.landscape.example.json").read_text())
    settings.update(clips=4, candidates=9, asr_parts=3, max_source_seconds=43200)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("SETTINGS_JSON", json.dumps(settings))
    code = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    exec(compile(code, "preview-settings", "exec"), {})
    config = tmp_path / "preview-settings.json"
    assert json.loads(config.read_text()) == settings
    assert config.stat().st_mode & 0o777 == 0o600


def test_cookie_secret_is_scoped_to_retrieval_steps():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    allowed = {
        "Acquire and verify the audio before GPU allocation",
        "Verify this run's analysis and create previews and an unapproved draft",
    }
    seen = set()
    for job in workflow["jobs"].values():
        assert "YOUTUBE_COOKIES_B64" not in job.get("env", {})
        for step in job["steps"]:
            if "YOUTUBE_COOKIES_B64" in step.get("env", {}):
                assert step.get("name") in allowed
                seen.add(step["name"])
    assert seen == allowed


def colab_step(name):
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    steps = workflow["jobs"]["colab-auto"]["steps"]
    return next(step for step in steps if step.get("name") == name)


FETCH = "Download the requested verified audio artifact from a successful producer run"
ACQUIRE = "Acquire and verify the audio before GPU allocation"
VERIFIED_REQUEST = "c" * 32


def test_verified_audio_reuse_inputs_are_optional_and_wired_before_acquisition():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    inputs = workflow.get(True, workflow.get("on"))["workflow_dispatch"]["inputs"]
    for name in ("verified_audio_run", "verified_audio_request"):
        assert inputs[name]["type"] == "string" and inputs[name]["default"] == ""
    steps = workflow["jobs"]["colab-auto"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index(FETCH) < names.index(ACQUIRE)
    fetch = colab_step(FETCH)
    assert (
        fetch["if"]
        == "inputs.verified_audio_run != '' || inputs.verified_audio_request != ''"
    )
    assert fetch["timeout-minutes"] == 5
    assert "gh release download" not in fetch["run"]
    assert "clipforge-verified-audio-' + request" in fetch["run"]
    acquire = colab_step(ACQUIRE)
    assert acquire["if"] == "inputs.audio_url != ''"
    assert acquire["env"]["VERIFIED_AUDIO_RUN"] == "${{ inputs.verified_audio_run }}"
    assert (
        acquire["env"]["VERIFIED_AUDIO_REQUEST"]
        == "${{ inputs.verified_audio_request }}"
    )


def run_step_script(script, cwd, env):
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def workflow_shell(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$@" >> "$GH_LOG"\n'
        'if [ "$1" = api ]; then printf "%s" "$FAKE_RUN_JSON"; fi\n'
    )
    gh.chmod(0o755)
    work = tmp_path / "work"
    python = work / "clipforge-source/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_LOG"\n')
    python.chmod(0o755)
    runner = tmp_path / "runner"
    runner.mkdir()

    def env(**values):
        base = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner),
            "GITHUB_REPOSITORY": "nyu1791-collab/ai-model-workers",
            "GH_LOG": str(tmp_path / "gh.log"),
            "ARGV_LOG": str(tmp_path / "argv.log"),
            "INPUT_TAG": "clipforge-input-" + "b" * 32,
            "REQUEST_ID": "b" * 32,
            "CLIPFORGE_SOURCE_REF": "a" * 40,
            "AUDIO_URL": URL,
            "SETTINGS_JSON": "{}",
            "AUTO_PREVIEWS": "false",
            "VERIFIED_AUDIO_RUN": "",
            "VERIFIED_AUDIO_REQUEST": "",
            "GH_TOKEN": "synthetic",
            "FAKE_RUN_JSON": "{}",
        }
        base.update(values)
        return base

    return work, runner, env, tmp_path


def test_acquire_command_is_unchanged_without_verified_audio(workflow_shell):
    work, runner, env, tmp_path = workflow_shell
    result = run_step_script(colab_step(ACQUIRE)["run"], work, env())
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "argv.log").read_text().splitlines() == [
        "clipforge-source/tools/acquire_audio_url.py",
        "--url",
        URL,
        "--request-id",
        "b" * 32,
        "--source-ref",
        "a" * 40,
        "--settings-json",
        "{}",
        "--directory",
        f"{runner}/clipforge-input",
    ]
    assert "--verified-audio" not in (tmp_path / "argv.log").read_text()


def test_acquire_passes_verified_audio_arguments_only_when_requested(workflow_shell):
    work, runner, env, tmp_path = workflow_shell
    result = run_step_script(
        colab_step(ACQUIRE)["run"],
        work,
        env(VERIFIED_AUDIO_RUN="123", VERIFIED_AUDIO_REQUEST=VERIFIED_REQUEST),
    )
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv.log").read_text().splitlines()
    assert argv[argv.index("--verified-audio-dir") :] == [
        "--verified-audio-dir",
        f"{runner}/clipforge-verified-audio",
        "--verified-audio-run",
        "123",
        "--verified-audio-request",
        VERIFIED_REQUEST,
        "--verified-audio-source-ref",
        "a" * 40,
    ]


def fetch(workflow_shell, run, request, producer, audio_url=URL):
    work, runner, env, tmp_path = workflow_shell
    result = run_step_script(
        colab_step(FETCH)["run"],
        work,
        env(
            VERIFIED_AUDIO_RUN=run,
            VERIFIED_AUDIO_REQUEST=request,
            AUDIO_URL=audio_url,
            FAKE_RUN_JSON=json.dumps(producer),
        ),
    )
    log = (tmp_path / "gh.log").read_text() if (tmp_path / "gh.log").exists() else ""
    return result, log, runner


def test_fetch_downloads_only_a_successful_matching_producer_run(workflow_shell):
    producer = {
        "conclusion": "success",
        "display_title": "ClipForge " + VERIFIED_REQUEST,
    }
    result, log, runner = fetch(workflow_shell, "123", VERIFIED_REQUEST, producer)
    assert result.returncode == 0, result.stderr
    assert (
        f"run download 123 --repo nyu1791-collab/ai-model-workers "
        f"--name clipforge-verified-audio-{VERIFIED_REQUEST} "
        f"--dir {runner}/clipforge-verified-audio"
    ) in log
    assert (runner / "clipforge-verified-audio").is_dir()


@pytest.mark.parametrize(
    ("run", "verified_request", "producer", "audio_url"),
    [
        (
            "123",
            VERIFIED_REQUEST,
            {"conclusion": "failure", "display_title": "ClipForge " + VERIFIED_REQUEST},
            URL,
        ),
        (
            "123",
            VERIFIED_REQUEST,
            {"conclusion": "success", "display_title": "ClipForge " + "d" * 32},
            URL,
        ),
        (
            "0",
            VERIFIED_REQUEST,
            {"conclusion": "success", "display_title": "ClipForge " + VERIFIED_REQUEST},
            URL,
        ),
        (
            "123",
            "not-hex",
            {"conclusion": "success", "display_title": "ClipForge not-hex"},
            URL,
        ),
        (
            "123",
            VERIFIED_REQUEST,
            {"conclusion": "success", "display_title": "ClipForge " + VERIFIED_REQUEST},
            "",
        ),
    ],
)
def test_fetch_refuses_unverified_or_mismatched_producers_before_download(
    workflow_shell, run, verified_request, producer, audio_url
):
    result, log, _ = fetch(workflow_shell, run, verified_request, producer, audio_url)
    assert result.returncode != 0
    assert "run download" not in log
