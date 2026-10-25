import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from clipforge import youtube_transport as transport
from clipforge.storage import PipelineError

URL = "https://youtu.be/6ras3m-gFuc"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(tmp_path / "github-env"),
    }.items():
        monkeypatch.setenv(name, value)
    network = {
        "warp": False,
        "fail_connect": False,
        "container": False,
        "foreign": False,
        "running": True,
    }
    monkeypatch.setattr(
        transport,
        "route",
        lambda: {
            "warp": network["warp"],
            "egress_sha256": "warp-ip-hash" if network["warp"] else "direct-ip-hash",
        },
    )
    monkeypatch.setattr(
        transport,
        "health",
        lambda: {"version": transport.PROVIDER_VERSION, "server_uptime": 0.1},
    )
    monkeypatch.setattr(transport.time, "sleep", lambda seconds: None)

    class Runtime(transport.Transport):
        def __init__(self, *args):
            super().__init__(*args)
            self.calls = []

        def command(self, args, **kwargs):
            self.calls.append(args)
            output = ""
            if args[0] == sys.executable:
                output = json.dumps(
                    {
                        "yt-dlp": transport.YT_DLP_VERSION,
                        "bgutil-ytdlp-pot-provider": transport.PROVIDER_VERSION,
                        "curl-cffi": "0.13.0",
                        "yt-dlp-ejs": "0.8.0",
                    }
                )
            elif args[0] == "node":
                output = "v" + transport.NODE_VERSION
            elif args[:2] == ["docker", "run"]:
                network["container"] = True
            elif args[:2] == ["docker", "inspect"]:
                output = (
                    str(network["running"]).lower()
                    if args[3] == "{{.State.Running}}"
                    else (
                        "foreign"
                        if network["foreign"]
                        else self.owner + " " + self.state["instance"]
                    )
                )
            elif args[:2] == ["docker", "ps"]:
                output = self.container if network["container"] else ""
            elif args[:2] == ["docker", "rm"]:
                network["container"] = False
            elif args[0] == "bash":
                if network["fail_connect"]:
                    raise PipelineError("private-secret-provider-body")
                network["warp"] = True
            elif args[0] == "dpkg-query":
                output = transport.WARP_VERSION
            elif args[0] == "timeout":
                network["warp"] = False
            return subprocess.CompletedProcess(args, 0, output, "")

    instance = Runtime(tmp_path / "transport", URL)
    return instance, network, Runtime


def test_owned_lifecycle_pins_versions_resets_after_route_change_and_cleans(runtime):
    control, network, _ = runtime
    control.prepare()
    assert control.state["status"] == "prepared"
    control.connect()
    assert control.verify()["status"] == "ready"
    actions = [call[0] for call in control.calls]
    connect = actions.index("bash")
    restart = next(
        i for i, call in enumerate(control.calls) if call[:2] == ["docker", "restart"]
    )
    assert restart > connect
    assert any("--require-hashes" in call for call in control.calls)
    assert any(
        transport.PROVIDER_IMAGE in call and "@sha256:" in " ".join(call)
        for call in control.calls
    )
    assert control.path.stat().st_mode & 0o777 == 0o600
    assert URL not in control.path.read_text()
    control.cleanup()
    assert control.state["status"] == "cleaned"
    assert not network["warp"] and not network["container"]
    calls = len(control.calls)
    control.cleanup()
    assert len(control.calls) == calls


def test_route_change_refuses_cached_tokens_without_automatic_fallback(
    runtime, monkeypatch
):
    control, _, _ = runtime
    control.prepare()
    control.connect()
    calls = len(control.calls)
    monkeypatch.setattr(
        transport, "route", lambda: {"warp": True, "egress_sha256": "different-ip"}
    )
    with pytest.raises(PipelineError, match="route changed"):
        control.verify()
    assert len(control.calls) == calls


def test_existing_warp_is_preserved_and_never_claimed(runtime):
    control, network, _ = runtime
    network["warp"] = True
    with pytest.raises(PipelineError, match="already exists"):
        control.prepare()
    control.cleanup()
    assert network["warp"]
    assert not control.calls


def test_foreign_container_is_never_removed(runtime):
    control, network, _ = runtime
    control.prepare()
    network["foreign"] = True
    with pytest.raises(PipelineError, match="cleanup failed"):
        control.cleanup()
    assert network["container"]
    assert not any(call[:2] == ["docker", "rm"] for call in control.calls)
    assert control.state["status"] == "cleanup_failed"


def test_foreign_run_and_source_are_rejected_before_commands(runtime, monkeypatch):
    control, _, Runtime = runtime
    control.prepare()
    with pytest.raises(PipelineError, match="another source"):
        Runtime(control.root, "https://youtu.be/abcdefghijk")
    monkeypatch.setenv("GITHUB_RUN_ID", "456")
    with pytest.raises(PipelineError, match="another run"):
        Runtime(control.root, URL)


def test_wrong_health_version_fails_before_connection(runtime, monkeypatch):
    control, _, _ = runtime
    monkeypatch.setattr(transport, "health", lambda: {"version": "unknown"})
    with pytest.raises(PipelineError, match="unexpected PO Token"):
        control.prepare()
    assert not any(call[0] == "bash" for call in control.calls)
    control.cleanup()


def test_transport_cannot_run_on_developer_host_or_outside_runner_temp(
    runtime, monkeypatch, tmp_path
):
    _, _, Runtime = runtime
    with pytest.raises(PipelineError, match="RUNNER_TEMP"):
        Runtime(tmp_path.parent / "outside", URL)
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    with pytest.raises(PipelineError, match="disposable"):
        Runtime(tmp_path / "state", URL)


def test_cli_failure_cleans_resources_and_preserves_phase_without_secrets(
    runtime, monkeypatch, capsys
):
    control, network, _ = runtime
    network["fail_connect"] = True
    spec = importlib.util.spec_from_file_location(
        "youtube_transport_cli", ROOT / "tools/youtube_transport.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "Transport", lambda *args: control)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "youtube_transport.py",
            "setup",
            "--state-dir",
            str(control.root),
            "--url",
            URL,
        ],
    )
    assert module.main() == 1
    assert not network["warp"] and not network["container"]
    saved = json.loads(control.path.read_text())
    assert saved["failure"]["phase"] == "warp_connect"
    assert saved["status"] == "cleaned"
    output = capsys.readouterr()
    assert "private-secret" not in output.out + output.err + control.path.read_text()


def test_subcommands_do_not_receive_cookie_or_model_credentials(runtime, monkeypatch):
    control, _, _ = runtime
    control.state = {"phase": "test"}
    for name in ("YOUTUBE_COOKIES_B64", "GEMINI_API_KEY", "GH_TOKEN", "COLAB_ADC_JSON"):
        monkeypatch.setenv(name, "private-test-value")
    monkeypatch.setenv("HTTPS_PROXY", "https://normal-proxy.example")
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(transport.subprocess, "run", run)
    transport.Transport.command(control, ["node", "--version"])
    assert "private-test-value" not in json.dumps(seen)
    assert seen[0]["HTTPS_PROXY"] == "https://normal-proxy.example"


def test_main_workflow_ends_transport_before_any_gpu_and_keeps_receipt_private():
    workflow = yaml.safe_load((ROOT / "deploy/clipforge.yml").read_text())
    steps = workflow["jobs"]["colab-auto"]["steps"]
    setup = next(
        i for i, step in enumerate(steps) if step.get("id") == "youtube_transport"
    )
    acquire = next(
        i
        for i, step in enumerate(steps)
        if "tools/acquire_audio_url.py" in step.get("run", "")
    )
    close = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Close YouTube audio transport before allocating a GPU"
    )
    allocate = next(
        i
        for i, step in enumerate(steps)
        if "colab_actions.py start" in step.get("run", "")
    )
    assert setup < acquire < close < allocate
    assert "youtube_audio_transport == 'warp'" in steps[setup]["if"]
    assert "always()" in steps[close]["if"]
    assert "YOUTUBE_COOKIES_B64" not in json.dumps(steps[setup])


def test_warp_preflight_defers_metadata_to_verified_runner(monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(
        "preflight_warp", ROOT / "tools/submit_job.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from clipforge import media_sources

    def forbidden(*args, **kwargs):
        raise AssertionError("local preflight must not change route or contact YouTube")

    monkeypatch.setattr(media_sources, "RemoteMedia", forbidden)
    assert (
        module.preflight_audio_url(
            URL, {"gemini_model": "manual-no-api", "youtube_audio_transport": "warp"}
        )
        is None
    )
    with pytest.raises(PipelineError, match="requires a YouTube"):
        module.preflight_audio_url(
            "https://example.com/audio.mp3",
            {"gemini_model": "manual-no-api", "youtube_audio_transport": "warp"},
        )

    bad_cookies = tmp_path / "cookies.txt"
    bad_cookies.write_text("invalid private cookie content")
    with pytest.raises(PipelineError, match="invalid YouTube cookies"):
        module.preflight_audio_url(
            URL,
            {"gemini_model": "manual-no-api", "youtube_audio_transport": "warp"},
            cookies_file=bad_cookies,
        )


def test_dispatch_keeps_explicit_worker_ref_for_review_and_resume(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(
        "workflow_ref_dispatch", ROOT / "tools/submit_job.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(
        module,
        "gh",
        lambda *args, **kwargs: calls.append(json.loads(kwargs["body"])) or "",
    )
    receipt = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "input_tag": "clipforge-input-" + "a" * 32,
        "worker": "colab-auto",
        "workflow_ref": "feat/two-part-asr-media-sources",
    }
    module.send_dispatch(tmp_path, receipt)
    assert calls[0]["ref"] == "feat/two-part-asr-media-sources"
    assert (
        json.loads((tmp_path / "receipt.json").read_text())["workflow_ref"]
        == "feat/two-part-asr-media-sources"
    )
    for value in ("../other", "main\nmalformed", "https://example.com"):
        with pytest.raises(RuntimeError, match="workflow ref"):
            module.workflow_ref(value)
