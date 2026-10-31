import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys

import pytest

from clipforge.models import Segment, Transcript

REQUEST = "a" * 32
REF = "b" * 40
RUN = 4242
URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
KEY = "c" * 64
AUDIO = b"synthetic-audio"
AUDIO_SHA = hashlib.sha256(AUDIO).hexdigest()


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location("test_tool_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_result(root):
    """A topics_allocated artifact as the private worker returns it."""
    (root / "transcripts").mkdir(parents=True)
    (root / "audio.m4a").write_bytes(AUDIO)
    transcript = Transcript(
        language="ja", segments=[Segment(start=1, end=4, text="最初の話題")]
    )
    (root / "transcripts" / (KEY + ".json")).write_text(transcript.model_dump_json())
    (root / "asr-receipt.json").write_text(json.dumps({"key": KEY, "reused": True}))
    (root / "topics.json").write_text(
        json.dumps(
            {
                "status": "topics_allocated",
                "analysis_key": "d" * 64,
                "source_url": URL,
                "duration": 600.0,
                "topics": [
                    {
                        "id": "topic-001",
                        "start": 0.0,
                        "end": 120.0,
                        "speech_seconds": 90.0,
                        "text": "最初の話題",
                    }
                ],
                "model_generation_requests": 0,
            }
        )
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "status": "topics_allocated",
                "source_url": URL,
                "source_sha256": AUDIO_SHA,
                "rendered": [],
                "model_generation_requests": 0,
            }
        )
    )
    names = [
        "audio.m4a",
        "manifest.json",
        "topics.json",
        "asr-receipt.json",
        "transcripts/" + KEY + ".json",
    ]
    (root / "request-receipt.json").write_text(
        json.dumps(
            {
                "request_id": REQUEST,
                "source_ref": REF,
                "workflow_run": RUN,
                "status": "topics_allocated",
                "audio_source_url": URL,
                "sha256": {name: digest(root / name) for name in names},
            }
        )
    )


def run_row(**changes):
    row = {
        "id": RUN,
        "display_title": "ClipForge " + REQUEST,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
    }
    row.update(changes)
    return row


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """Stateless collect with a fake gh only; no network and no local record."""
    module = load_tool("submit_job")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = tmp_path / "artifact"
    make_result(artifact)
    control = {"run": run_row(), "calls": []}

    def gh(*args, body=None, timeout=None):
        control["calls"].append(args)
        if args[:2] == ("api", "user"):
            return "nyu1791-collab"
        if args[:1] == ("api",) and args[1] == "repos/" + module.REPO:
            return "true"
        if args[:2] == ("api", f"repos/{module.REPO}/actions/runs/{RUN}"):
            return json.dumps(control["run"])
        if args[:2] == ("run", "download"):
            shutil.copytree(
                artifact, Path(args[args.index("--dir") + 1]), symlinks=True
            )
            return ""
        raise AssertionError("unexpected gh call: " + str(args))

    monkeypatch.setattr(module, "gh", gh)
    return module, artifact, control, tmp_path, monkeypatch


def invoke(monkeypatch, module, *extra):
    monkeypatch.setattr(sys, "argv", ["submit_job", "--collect", REQUEST, *extra])
    module.main()


def standard_args(output, sha=AUDIO_SHA):
    return [
        "--run-id",
        str(RUN),
        "--source-ref",
        REF,
        "--audio-sha256",
        sha,
        "--output",
        str(output),
    ]


DIAGNOSTICS = ["gpu-receipt.json", "acquisition.json", "audio.m4a.receipt.json"]


@pytest.mark.parametrize("name", DIAGNOSTICS)
@pytest.mark.parametrize("kind", ["nested", "directory", "symlink", "fifo", "suffix"])
def test_stateless_collect_limits_diagnostic_exceptions(collector, name, kind):
    module, artifact, _, tmp_path, monkeypatch = collector
    target = artifact / name
    if kind == "nested":
        (artifact / "transcripts" / name).write_text("{}")
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(artifact / "audio.m4a")
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.with_suffix(".json.extra").write_text("{}")
    # Verify in place: copying a FIFO would fail before the verifier sees it.
    with pytest.raises(RuntimeError, match="unexpected|symlink|non-regular"):
        module.verify_audio_analysis(artifact, REQUEST, REF, AUDIO_SHA, URL)


def set_topic_end(artifact, end):
    path = artifact / "topics.json"
    data = json.loads(path.read_text())
    data["topics"][0]["end"] = end
    path.write_text(json.dumps(data))
    # Keep the receipt's checksum current so the topic row check is what is exercised.
    receipt_path = artifact / "request-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["sha256"]["topics.json"] = digest(path)
    receipt_path.write_text(json.dumps(receipt))


@pytest.mark.parametrize("excess", [0.0003, 0.01])
def test_stateless_collect_accepts_end_within_time_tolerance(collector, excess):
    module, artifact, _, _, _ = collector
    set_topic_end(artifact, 600.0 + excess)
    module.verify_audio_analysis(artifact, REQUEST, REF, AUDIO_SHA, URL)


def test_stateless_collect_rejects_end_beyond_time_tolerance(collector):
    module, artifact, _, _, _ = collector
    set_topic_end(artifact, 600.02)
    with pytest.raises(RuntimeError, match="topics file row format mismatch"):
        module.verify_audio_analysis(artifact, REQUEST, REF, AUDIO_SHA, URL)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("present", "REMAINS"),
        (RuntimeError("GitHub operation failed: api (HTTP 404)"), "DELETED"),
        (RuntimeError("GitHub operation failed: api (HTTP 502)"), "UNKNOWN"),
        (TimeoutError("gh did not answer"), "UNKNOWN"),
    ],
)
def test_input_cleanup_status_reports_whether_the_release_remains(
    monkeypatch, outcome, expected
):
    module = load_tool("submit_job")
    calls = []

    def gh(*args, **kwargs):
        calls.append(args)
        if isinstance(outcome, Exception):
            raise outcome
        return "clipforge-input-" + "a" * 32

    monkeypatch.setattr(module, "gh", gh)
    assert module.input_cleanup_status("clipforge-input-" + "a" * 32) == expected
    assert calls[0][:2] == (
        "api",
        "repos/nyu1791-collab/ai-model-workers/releases/tags/clipforge-input-"
        + "a" * 32,
    )


def test_input_cleanup_status_never_queries_an_unexpected_tag(monkeypatch):
    module = load_tool("submit_job")
    monkeypatch.setattr(
        module, "gh", lambda *a, **k: pytest.fail("no query for a bad tag")
    )
    assert module.input_cleanup_status("clipforge-cache-gpu-x") == "UNKNOWN"


def test_dispatch_forwards_verified_audio_reuse_only_when_requested(
    tmp_path, monkeypatch
):
    module = load_tool("submit_job")
    posted = []
    repo, source = module.REPO, module.SOURCE

    def gh(*args, body=None, timeout=None):
        if args[0] == "api" and "--method" in args:
            posted.append(json.loads(body))
            return ""
        if args[0] == "api" and args[1].startswith(f"repos/{source}/commits/"):
            return json.dumps({"sha": REF})
        if args[0] == "api" and args[1].startswith(f"repos/{repo}/actions/workflows/"):
            return json.dumps({"state": "active"})
        if args[0] == "run":
            return "[]"
        if args[0] == "secret":
            return json.dumps([{"name": "COLAB_ADC_JSON"}, {"name": "GEMINI_API_KEY"}])
        raise AssertionError(args)

    monkeypatch.setattr(module, "gh", gh)
    monkeypatch.setattr(
        module, "preflight_audio_url", lambda *a, **k: {"has_video": True}
    )

    def submit_with(**reuse):
        args = SimpleNamespace(
            config=None,
            audio_analysis_url=URL,
            clips=None,
            ref="main",
            auto_previews=False,
            asr_parts=None,
            youtube_audio_transport=None,
            cookies_file=None,
            workflow_ref=None,
            **reuse,
        )
        return module.dispatch(args, tmp_path)

    state, receipt = submit_with(reuse_audio_run=None, reuse_audio_request=None)
    assert "verified_audio_run" not in receipt
    assert "verified_audio_run" not in posted[-1]["inputs"]
    assert "verified_audio_request" not in posted[-1]["inputs"]

    verified = "c" * 32
    state, receipt = submit_with(reuse_audio_run=123, reuse_audio_request=verified)
    assert (
        json.loads((state / "receipt.json").read_text())["verified_audio_run"] == "123"
    )
    assert receipt["verified_audio_request"] == verified
    assert posted[-1]["inputs"]["verified_audio_run"] == "123"
    assert posted[-1]["inputs"]["verified_audio_request"] == verified
    assert posted[-1]["inputs"]["request_id"] == receipt["request_id"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--reuse-audio-run", "123"],
        ["--reuse-audio-request", "c" * 32],
        ["--reuse-audio-run", "0", "--reuse-audio-request", "c" * 32],
        ["--reuse-audio-run", "123", "--reuse-audio-request", "not-hex"],
        [
            "--reuse-audio-run",
            "123",
            "--reuse-audio-request",
            "c" * 32,
            "--status",
            "c" * 32,
        ],
    ],
)
def test_reuse_arguments_are_refused_before_any_github_call(monkeypatch, extra):
    module = load_tool("submit_job")
    monkeypatch.setattr(module, "gh", lambda *a, **k: pytest.fail("gh was called"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            URL,
            *extra,
        ],
    )
    with pytest.raises(SystemExit):
        module.main()


def test_reuse_arguments_reach_submission(monkeypatch):
    module = load_tool("submit_job")
    seen = {}
    monkeypatch.setattr(
        module,
        "gh",
        lambda *a, **k: "nyu1791-collab" if a[1] == "user" else "true",
    )
    monkeypatch.setattr(module, "submit", lambda args: seen.update(vars(args)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            URL,
            "--reuse-audio-run",
            "123",
            "--reuse-audio-request",
            REQUEST,
        ],
    )
    module.main()
    assert seen["reuse_audio_run"] == 123
    assert seen["reuse_audio_request"] == REQUEST


@pytest.fixture(autouse=True)
def legacy_artifact_repository(monkeypatch):
    """These fixtures describe older runs without Release stage transfers."""
    from clipforge import release_transfer

    monkeypatch.setattr(release_transfer, "private_repo", lambda: None)
    monkeypatch.setattr(release_transfer, "release", lambda tag: None)
