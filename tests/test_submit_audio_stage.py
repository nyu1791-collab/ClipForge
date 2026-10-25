import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

from clipforge.storage import PipelineError


@pytest.fixture
def stage_submitter(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "submit_audio_stage", Path("tools/submit_audio_stage.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path))
    control = {
        "private": True,
        "runs": [],
        "list_args": [],
        "posts": [],
        "post_args": [],
        "timeout": False,
        "configs": {
            "config.landscape.example.json",
            "config.landscape-chat.example.json",
        },
    }

    def gh(*args, body=None):
        if args[:2] == ("api", "user"):
            return "nyu1791-collab"
        if args[:1] == ("api",) and args[1].count("/") == 2 and "--jq" in args:
            return "true" if control["private"] else "false"
        if args[:2] == ("repo", "view"):
            return json.dumps({"isPrivate": control["private"]})
        if args[:2] == ("run", "list"):
            # Mirror gh: --workflow restricts the listing to that workflow's runs.
            control["list_args"].append(args)
            rows = control["runs"]
            if "--workflow" in args:
                name = args[args.index("--workflow") + 1]
                rows = [row for row in rows if row["workflow"] == name]
            if "--branch" in args:
                branch = args[args.index("--branch") + 1]
                rows = [row for row in rows if row.get("headBranch", "main") == branch]
            return json.dumps([{"status": row["status"]} for row in rows])
        control["post_args"].append(args)
        control["posts"].append(json.loads(body))
        if control["timeout"]:
            raise TimeoutError("reply lost")
        return ""

    monkeypatch.setattr(module, "gh", gh)
    # Fake refs have no committed files; the real check is tested separately.
    control["real_config_in_source"] = module.config_in_source
    monkeypatch.setattr(
        module,
        "config_in_source",
        lambda source_ref, name: name in control["configs"],
    )
    args = [
        "--stage",
        "previews",
        "--previous-run",
        "123",
        "--previous-request",
        "b" * 32,
        "--source-ref",
        "a" * 40,
    ]
    return module, control, args


def test_stage_identity_prevents_duplicate_dispatch(stage_submitter):
    module, control, args = stage_submitter
    assert module.main(args) == 0
    payload = control["posts"][0]
    assert payload["ref"] == "main"
    assert control["post_args"][0][3] == (
        f"repos/{module.REPO}/actions/workflows/clipforge-stages.yml/dispatches"
    )
    assert payload["inputs"]["previous_request_id"] == "b" * 32
    with pytest.raises(RuntimeError, match="already has a request"):
        module.main(args)
    assert len(control["posts"]) == 1


def test_render_submission_accepts_the_chained_run_and_request(
    stage_submitter, tmp_path
):
    module, control, args = stage_submitter
    args[1] = "render"
    review = tmp_path / "reviewed.json"
    review.write_text(
        json.dumps(
            {
                "analysis_key": "c" * 64,
                "clips": [{"candidate_id": "topic-001", "video_reviewed": True}],
            }
        )
    )
    # The chained previews keep the original analysis run and request identities.
    assert module.main([*args, "--review-file", str(review)]) == 0
    inputs = control["posts"][0]["inputs"]
    assert inputs["audio_stage"] == "render"
    assert inputs["previous_run_id"] == "123"
    assert inputs["previous_request_id"] == "b" * 32


def test_active_clipforge_run_blocks_dispatch(stage_submitter):
    module, control, args = stage_submitter
    control["runs"] = [{"workflow": module.WORKFLOW, "status": "in_progress"}]
    with pytest.raises(RuntimeError, match="another worker is active"):
        module.main(args)
    assert not control["posts"]


def test_unrelated_workflow_run_does_not_block_dispatch(stage_submitter):
    module, control, args = stage_submitter
    control["runs"] = [{"workflow": "other.yml", "status": "in_progress"}]
    assert module.main(args) == 0
    assert len(control["posts"]) == 1
    run_list = [a for a in control["list_args"] if a[:2] == ("run", "list")]
    assert run_list and run_list[-1][run_list[-1].index("--workflow") + 1] == (
        "clipforge-stages.yml"
    )
    assert run_list[-1][run_list[-1].index("--branch") + 1] == "main"


def test_other_branch_does_not_block_new_stage_workflow(stage_submitter):
    module, control, args = stage_submitter
    control["runs"] = [
        {
            "workflow": module.WORKFLOW,
            "headBranch": "other-branch",
            "status": "in_progress",
        }
    ]
    assert module.main(args) == 0


@pytest.mark.parametrize(
    "fault", ["active", "public", "missing_review", "invalid_source"]
)
def test_invalid_stage_never_starts_a_worker(stage_submitter, fault):
    module, control, args = stage_submitter
    if fault == "active":
        control["runs"] = [{"workflow": module.WORKFLOW, "status": "in_progress"}]
    elif fault == "public":
        control["private"] = False
    elif fault == "missing_review":
        args[1] = "render"
    else:
        args[-1] = "main"
    with pytest.raises(RuntimeError):
        module.main(args)
    assert not control["posts"]


def test_unconfirmed_dispatch_is_not_sent_again(stage_submitter):
    module, control, args = stage_submitter
    control["timeout"] = True
    with pytest.raises(TimeoutError):
        module.main(args)
    with pytest.raises(RuntimeError, match="already has a request"):
        module.main(args)
    assert len(control["posts"]) == 1


def test_command_timeout_exits_with_short_failure_message(
    collector, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["submit_audio_stage", *collect_args(tmp_path / "collected")]
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            ["gh", "synthetic-command"],
            60,
            output="synthetic hidden output",
            stderr="synthetic hidden error",
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path("tools/submit_audio_stage.py", run_name="__main__")
    assert exit_info.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        "Audio stage command failed: TimeoutExpired: timed out\n"
        "依頼が作られた後の失敗なら ~/.local/state/clipforge/<依頼ID>/receipt.json を確認する。"
        "自動の再送や代替は行っていない。\n"
    )


@pytest.mark.parametrize(
    "make_error, shown",
    [
        (
            lambda: RuntimeError("the approved owner connection is required"),
            "RuntimeError",
        ),
        (
            lambda: PipelineError("SVG is larger than the allowed size"),
            "PipelineError",
        ),
        (lambda: ValueError("bad\nvalue"), "ValueError"),
        (lambda: ValueError("x" * 400), "ValueError"),
        (lambda: KeyError("row"), "KeyError"),
        (lambda: TypeError("expected str"), "TypeError"),
        (
            lambda: FileNotFoundError(
                2, "No such file or directory", "/private/secret/a.json"
            ),
            "FileNotFoundError: No such file or directory",
        ),
        (
            lambda: OSError(13, "Permission denied", "/private/secret"),
            "PermissionError: Permission denied",
        ),
        (
            lambda: subprocess.CalledProcessError(
                1, ["gh", "api", "https://example.test/x?token=SECRET"], output="SECRET"
            ),
            "CalledProcessError: exit 1",
        ),
        (
            lambda: subprocess.TimeoutExpired(
                ["gh", "https://example.test/signed?sig=SECRET"], 60
            ),
            "TimeoutExpired: timed out",
        ),
    ],
)
def test_run_cli_shows_error_type_without_paths_commands_or_urls(
    stage_submitter, monkeypatch, capsys, make_error, shown
):
    module, _, _ = stage_submitter

    def fail(argv=None):
        raise make_error()

    monkeypatch.setattr(module, "main", fail)
    assert module.run_cli(["--stage", "merge"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    first, second, *rest = output.err.splitlines()
    assert first == "Audio stage command failed: " + shown
    assert second == module.RECEIPT_HINT
    assert rest == []
    for leaked in ("/private", "gh ", "http", "SECRET", "token", "sig="):
        assert leaked not in output.err


COLLECT_REQUEST = "d" * 32
PREVIOUS_RUN = "123"
PREVIOUS_REQUEST = "b" * 32
SOURCE_REF = "a" * 40
KICK_URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
RUN_ID = 4567
ASR_KEY = "e" * 64


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_artifact(root, generated_sha=None, drop=(), **receipt_overrides):
    """Mirror the worker output: listed files, a generated receipt and request-receipt.json."""
    files = {
        "audio.m4a": b"synthetic audio",
        "topics.json": b'{"topics": []}',
        "previews.json": b'{"previews": []}',
        "asr-receipt.json": json.dumps({"key": ASR_KEY, "reused": True}).encode(),
        "transcripts/" + ASR_KEY + ".json": b'{"language": "ja", "segments": []}',
        "previews/topic-001.mp4": b"synthetic preview",
    }
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    generated = {
        "source_url": KICK_URL,
        "sha256": generated_sha or sha(root / "previews/topic-001.mp4"),
    }
    (root / "previews/topic-001.mp4.receipt.json").write_text(json.dumps(generated))
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "status": "awaiting_video_review",
                "audio_stage": "previews",
                "source_url": KICK_URL,
                "source_sha256": sha(root / "audio.m4a"),
                "asr_reused": True,
                "rendered": [],
            }
        )
    )
    hashes = {
        str(path.relative_to(root)): sha(path)
        for path in root.rglob("*")
        if path.is_file()
    }
    receipt = {
        "request_id": COLLECT_REQUEST,
        "source_ref": SOURCE_REF,
        "workflow_run": str(RUN_ID),
        "status": "awaiting_video_review",
        "audio_source_url": KICK_URL,
        "sha256": hashes,
        "asr_reused": True,
        "audio_stage": "previews",
        "previous_run_id": PREVIOUS_RUN,
        "previous_request_id": PREVIOUS_REQUEST,
    }
    receipt.update(receipt_overrides)
    for key in drop:
        receipt.pop(key)
    (root / "request-receipt.json").write_text(json.dumps(receipt))


@pytest.fixture
def collector(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "submit_audio_stage", Path("tools/submit_audio_stage.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "home/.local/state/clipforge" / COLLECT_REQUEST
    state.mkdir(parents=True)
    (state / "request.json").write_text(
        json.dumps(
            {
                "request_id": COLLECT_REQUEST,
                "input_tag": "clipforge-input-" + COLLECT_REQUEST,
                "source_ref": SOURCE_REF,
                "audio_stage": "previews",
                "previous_run_id": PREVIOUS_RUN,
                "previous_request_id": PREVIOUS_REQUEST,
            }
        )
    )
    (state / "receipt.json").write_text(
        json.dumps(
            {
                "request_id": COLLECT_REQUEST,
                "repository": module.REPO,
                "worker_ref": module.BRANCH,
                "source_ref": SOURCE_REF,
                "audio_stage": "previews",
                "previous_run_id": PREVIOUS_RUN,
                "status": "DISPATCHED",
                "job_submitted": True,
            }
        )
    )
    artifact = tmp_path / "artifact"
    build_artifact(artifact)
    control = {
        "artifact": artifact,
        "mutate": None,
        "downloads": 0,
        "list_args": [],
        "run": {
            "databaseId": RUN_ID,
            "displayTitle": "ClipForge " + COLLECT_REQUEST,
            "event": "workflow_dispatch",
            "headBranch": module.BRANCH,
            "status": "completed",
            "conclusion": "success",
        },
    }

    def gh(*args, body=None, timeout=60):
        if args[:2] == ("run", "list"):
            control["list_args"].append(args)
            return json.dumps([control["run"]])
        if args[:2] == ("run", "download"):
            control["downloads"] += 1
            staging = Path(args[args.index("--dir") + 1])
            shutil.copytree(control["artifact"], staging)
            if control["mutate"]:
                control["mutate"](staging)
            return ""
        raise AssertionError("unexpected mocked gh command: " + str(args))

    monkeypatch.setattr(module, "gh", gh)
    return module, control, state


def collect_args(destination):
    return ["--collect", COLLECT_REQUEST, "--output", str(destination)]


def build_delivery_collector(collector, stage):
    module, control, state = collector
    status = "complete" if stage == "merge" else "clip_rendered"
    parts = ["c" * 32, "f" * 32]
    for name in ("request.json", "receipt.json"):
        path = state / name
        saved = json.loads(path.read_text())
        saved.update(audio_stage=stage, clip_index="" if stage == "merge" else "2")
        if name == "request.json":
            saved["part_runs"] = "11:" + parts[0] + ",12:" + parts[1]
        path.write_text(json.dumps(saved))
    root = control["artifact"].with_name("delivery-artifact")
    rendered = []
    for index in [1, 2] if stage == "merge" else [2]:
        directory = f"delivery/clips/clip-{index:03}"
        clip = root / directory
        clip.mkdir(parents=True)
        for name in ("video.mp4", "captions.ass", "captions.srt", "quality.json"):
            (clip / name).write_bytes(b"synthetic clip output")
        rendered.append(
            {
                "directory": directory,
                "sha256": {path.name: sha(path) for path in clip.iterdir()},
            }
        )
    manifest = {
        "status": status,
        "audio_stage": "render",
        "source_url": KICK_URL,
        "source_sha256": "0" * 64,
        "rendered": rendered,
    }
    if stage == "render":
        manifest["clip_index"] = 2
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "delivery/manifest.json").write_text(json.dumps(manifest))
    if stage == "merge":
        (root / "delivery/e2e-summary.json").write_text("{}")
    receipt = {
        "request_id": COLLECT_REQUEST,
        "source_ref": SOURCE_REF,
        "workflow_run": str(RUN_ID),
        "status": status,
        "audio_source_url": KICK_URL,
        "sha256": {
            path.relative_to(root).as_posix(): sha(path)
            for path in root.rglob("*")
            if path.is_file()
        },
        "asr_reused": True,
        "audio_stage": stage,
        "previous_request_id": PREVIOUS_REQUEST,
    }
    if stage == "merge":
        receipt["part_request_ids"] = list(reversed(parts))
    else:
        receipt.update(previous_run_id=PREVIOUS_RUN, clip_index=2)
    (root / "request-receipt.json").write_text(json.dumps(receipt))
    control["artifact"] = root
    return module, control, state


@pytest.fixture(params=["merge", "render"])
def delivery_collector(collector, request):
    return build_delivery_collector(collector, request.param)


def test_collect_delivery_without_analysis_files(delivery_collector, tmp_path):
    module, control, state = delivery_collector
    destination = tmp_path / "collected"
    assert module.main(collect_args(destination)) == 0
    assert (destination / "delivery/clips/clip-002/video.mp4").read_bytes() == (
        b"synthetic clip output"
    )
    assert not (destination / "audio.m4a").exists()
    assert not (destination / "transcripts").exists()
    saved = json.loads((state / "receipt.json").read_text())
    assert saved["status"] == "COLLECTED" and saved["run_id"] == RUN_ID
    assert control["downloads"] == 1


def test_colab_published_clip_can_be_collected_and_detects_changed_report(
    collector, tmp_path, monkeypatch
):
    module, control, state = build_delivery_collector(collector, "render")
    monkeypatch.syspath_prepend(str(Path("tools").resolve()))
    import colab_remote_job

    root = control["artifact"]
    request = json.loads((state / "request.json").read_text())
    bundles = Mock()
    bundles.publish.side_effect = lambda *args: module.verify_stage_artifact(
        root, COLLECT_REQUEST, "render", request, RUN_ID
    )
    assert colab_remote_job.publish_result(
        bundles,
        {
            "input_tag": "clipforge-input-" + COLLECT_REQUEST,
            "request_id": COLLECT_REQUEST,
            "source_ref": SOURCE_REF,
        },
        root,
        {"mode": "audio-stage", "source_ref": SOURCE_REF, "status": "clip_rendered"},
        {},
    )
    bundles.publish.assert_called_once()
    assert module.main(collect_args(tmp_path / "collected")) == 0
    (root / "gpu-receipt.json").write_text("{}")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        module.verify_stage_artifact(root, COLLECT_REQUEST, "render", request, RUN_ID)


@pytest.mark.parametrize("clip_index", ["1", "3"])
def test_collect_single_clip_rejects_requested_index_mismatch(
    collector, tmp_path, clip_index
):
    module, _, state = build_delivery_collector(collector, "render")
    path = state / "request.json"
    saved = json.loads(path.read_text())
    saved["clip_index"] = clip_index
    path.write_text(json.dumps(saved))
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="clip index mismatch"):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_single_clip_rejects_multiple_rendered_rows(collector, tmp_path):
    module, control, _ = build_delivery_collector(collector, "render")
    root = control["artifact"]
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["rendered"] *= 2
    manifest_path.write_text(json.dumps(manifest))
    receipt_path = root / "request-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["sha256"]["manifest.json"] = sha(manifest_path)
    receipt_path.write_text(json.dumps(receipt))
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="clip index mismatch"):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_single_clip_accepts_directory_without_delivery_prefix(
    collector, tmp_path
):
    module, control, _ = build_delivery_collector(collector, "render")
    root = control["artifact"]
    (root / "delivery/clips").rename(root / "clips")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["rendered"][0]["directory"] = "clips/clip-002"
    manifest_path.write_text(json.dumps(manifest))
    receipt_path = root / "request-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["sha256"] = {
        path.relative_to(root).as_posix(): sha(path)
        for path in root.rglob("*")
        if path.is_file() and path != receipt_path
    }
    receipt_path.write_text(json.dumps(receipt))
    destination = tmp_path / "collected"
    assert module.main(collect_args(destination)) == 0
    assert (destination / "clips/clip-002/video.mp4").is_file()


@pytest.mark.parametrize(
    "overrides",
    [
        {"part_request_ids": ["c" * 32]},
        {"part_request_ids": ["c" * 32, "e" * 32]},
        {"part_request_ids": None},
        {"previous_request_id": "e" * 32},
        {"previous_request_id": None},
        {"previous_run_id": "999"},
    ],
)
def test_collect_merge_rejects_lineage_mismatch(collector, tmp_path, overrides):
    module, control, _ = build_delivery_collector(collector, "merge")
    path = control["artifact"] / "request-receipt.json"
    receipt = json.loads(path.read_text())
    receipt.update(overrides)
    path.write_text(json.dumps(receipt))
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="identity mismatch"):
        module.main(collect_args(destination))
    assert not destination.exists()


@pytest.mark.parametrize("key", ["previous_request_id", "part_request_ids"])
def test_collect_merge_requires_lineage(collector, tmp_path, key):
    module, control, _ = build_delivery_collector(collector, "merge")
    path = control["artifact"] / "request-receipt.json"
    receipt = json.loads(path.read_text())
    del receipt[key]
    path.write_text(json.dumps(receipt))
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="identity mismatch"):
        module.main(collect_args(destination))
    assert not destination.exists()


@pytest.mark.parametrize(
    "name",
    [
        "manifest.json",
        "delivery/clips/clip-002/video.mp4",
        "delivery/clips/clip-002/captions.ass",
        "delivery/clips/clip-002/captions.srt",
    ],
)
def test_collect_delivery_rejects_missing_required_file(
    delivery_collector, tmp_path, name
):
    module, control, state = delivery_collector
    root = control["artifact"]
    (root / name).unlink()
    path = root / "request-receipt.json"
    receipt = json.loads(path.read_text())
    del receipt["sha256"][name]
    path.write_text(json.dumps(receipt))
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="omits required files"):
        module.main(collect_args(destination))
    assert not destination.exists()
    assert json.loads((state / "receipt.json").read_text())["status"] == "DISPATCHED"


@pytest.mark.parametrize(
    "fault", ["extra", "hash", "unsafe", "invalid_sha", "status", "receipt_status"]
)
def test_collect_delivery_preserves_safety_checks(delivery_collector, tmp_path, fault):
    module, control, _ = delivery_collector
    root = control["artifact"]
    path = root / "request-receipt.json"
    receipt = json.loads(path.read_text())
    if fault == "extra":
        (root / "unlisted.txt").write_text("synthetic extra")
        error = "unlisted"
    elif fault == "hash":
        (root / "delivery/clips/clip-002/video.mp4").write_bytes(b"changed")
        error = "SHA-256 mismatch"
    elif fault == "unsafe":
        receipt["sha256"]["../escape"] = "a" * 64
        error = "unsafe path"
    elif fault == "invalid_sha":
        receipt["sha256"]["manifest.json"] = "invalid"
        error = "invalid SHA-256"
    elif fault == "receipt_status":
        receipt["status"] = "awaiting_video_review"
        error = "identity mismatch"
    else:
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["status"] = "awaiting_video_review"
        manifest_path.write_text(json.dumps(manifest))
        receipt["sha256"]["manifest.json"] = sha(manifest_path)
        error = "manifest does not match"
    path.write_text(json.dumps(receipt))
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match=error):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_verifies_listed_files_and_writes_new_directory(
    collector, tmp_path, capsys
):
    module, control, state = collector
    destination = tmp_path / "collected" / "stage"
    assert module.main(collect_args(destination)) == 0
    assert (destination / "previews/topic-001.mp4").read_bytes() == b"synthetic preview"
    assert (destination / "request-receipt.json").is_file()
    saved = json.loads((state / "receipt.json").read_text())
    assert saved["status"] == "COLLECTED" and saved["run_id"] == RUN_ID
    printed = capsys.readouterr().out
    assert KICK_URL not in printed and "sha256" not in printed
    assert control["downloads"] == 1


@pytest.mark.parametrize("clip_index", [None, ""])
def test_collect_full_render_keeps_analysis_requirements(
    collector, tmp_path, clip_index
):
    module, control, state = collector
    for name in ("request.json", "receipt.json"):
        path = state / name
        saved = json.loads(path.read_text())
        saved.update(audio_stage="render", clip_index=clip_index)
        path.write_text(json.dumps(saved))
    root = control["artifact"]
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(audio_stage="render", status="complete")
    manifest_path.write_text(json.dumps(manifest))
    receipt_path = root / "request-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(audio_stage="render", status="complete")
    receipt["sha256"]["manifest.json"] = sha(manifest_path)
    receipt_path.write_text(json.dumps(receipt))
    assert module.main(collect_args(tmp_path / "collected")) == 0
    (root / "audio.m4a").unlink()
    del receipt["sha256"]["audio.m4a"]
    receipt_path.write_text(json.dumps(receipt))
    destination = tmp_path / "incomplete"
    with pytest.raises(RuntimeError, match="omits required files"):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_rejects_sha_mismatch(collector, tmp_path):
    module, control, _ = collector
    control["mutate"] = lambda staging: (staging / "audio.m4a").write_bytes(b"changed")
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_rejects_missing_listed_file(collector, tmp_path):
    module, control, _ = collector
    control["mutate"] = lambda staging: (staging / "topics.json").unlink()
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="missing"):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_rejects_unlisted_extra_file(collector, tmp_path):
    module, control, _ = collector
    control["mutate"] = lambda staging: (staging / "unlisted.txt").write_text("extra")
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="unlisted"):
        module.main(collect_args(destination))
    assert not destination.exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute.bin", "previews/../../x"])
def test_collect_rejects_path_traversal_in_receipt(collector, tmp_path, name):
    module, control, _ = collector
    receipt_path = control["artifact"] / "request-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["sha256"][name] = "a" * 64
    receipt_path.write_text(json.dumps(receipt))
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="unsafe path"):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_never_overwrites_existing_destination(collector, tmp_path):
    module, control, _ = collector
    destination = tmp_path / "collected"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="already exists"):
        module.main(collect_args(destination))
    assert (destination / "keep.txt").read_text() == "keep"
    assert control["downloads"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": "c" * 32},
        {"source_ref": "c" * 40},
        {"previous_run_id": "999"},
        {"previous_request_id": "c" * 32},
        {"previous_run_id": None},
        {"workflow_run": "1"},
        {"generated_sha": "0" * 64},
    ],
)
def test_collect_rejects_identity_or_generated_receipt_mismatch(
    collector, tmp_path, overrides
):
    module, control, _ = collector
    bad = tmp_path / "bad"
    build_artifact(bad, **overrides)
    control["artifact"] = bad
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_accepts_matching_previous_run_and_request(collector, tmp_path):
    module, control, _ = collector
    destination = tmp_path / "collected"
    assert module.main(collect_args(destination)) == 0
    receipt = json.loads((destination / "request-receipt.json").read_text())
    assert receipt["previous_run_id"] == PREVIOUS_RUN
    assert receipt["previous_request_id"] == PREVIOUS_REQUEST


def test_collect_rejects_receipt_without_previous_keys(collector, tmp_path):
    module, control, _ = collector
    legacy = tmp_path / "legacy"
    build_artifact(legacy, drop=("previous_run_id", "previous_request_id"))
    control["artifact"] = legacy
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="identity"):
        module.main(collect_args(destination))
    assert not destination.exists()


def test_collect_requires_a_successful_stage_run(collector, tmp_path):
    module, control, _ = collector
    control["run"]["conclusion"] = "failure"
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="did not complete successfully"):
        module.main(collect_args(destination))
    assert control["downloads"] == 0 and not destination.exists()


def test_collect_rejects_retired_worker_branch_before_download(collector, tmp_path):
    module, control, state = collector
    path = state / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["worker_ref"] = "ccr-12fe9485-ugaszt"
    path.write_text(json.dumps(receipt))
    control["run"]["headBranch"] = receipt["worker_ref"]
    destination = tmp_path / "legacy-collected"
    with pytest.raises(RuntimeError, match="saved stage request identity mismatch"):
        module.main(collect_args(destination))
    assert not control["list_args"] and control["downloads"] == 0
    assert not destination.exists()


def render_args(args, tmp_path, *extra):
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"clips": [{"video_reviewed": True}] * 3}))
    return [
        *(["--stage", "render"] + args[2:]),
        "--review-file",
        str(review),
        *extra,
    ]


@pytest.mark.parametrize(
    "extra",
    [(), ("--clip-index", "1"), ("--worker", "colab-auto", "--clips", "3")],
)
@pytest.mark.parametrize("flag", [False, None, 1, "true", "missing"])
def test_render_rejects_unreviewed_drafts_before_any_remote_call(
    stage_submitter, tmp_path, monkeypatch, extra, flag
):
    module, control, args = stage_submitter
    argv = render_args(args, tmp_path, *extra)
    clips = [
        {"video_reviewed": True},
        {} if flag == "missing" else {"video_reviewed": flag},
    ]
    (tmp_path / "review.json").write_text(json.dumps({"clips": clips}))
    monkeypatch.setattr(
        module, "gh", lambda *a, **k: pytest.fail("unapproved review reached GitHub")
    )
    with pytest.raises(RuntimeError, match="requires video review"):
        module.main(argv)
    assert not control["posts"]
    assert not (tmp_path / ".local/state/clipforge").exists()


@pytest.mark.parametrize(
    "review", [[], {}, {"clips": []}, {"clips": "invalid"}, {"clips": [None]}]
)
def test_render_rejects_empty_or_malformed_reviews(stage_submitter, tmp_path, review):
    module, control, args = stage_submitter
    argv = render_args(args, tmp_path)
    (tmp_path / "review.json").write_text(json.dumps(review))
    with pytest.raises(RuntimeError, match="requires video review"):
        module.main(argv)
    assert not control["posts"]


def test_parallel_clips_dispatch_one_colab_request_per_clip(stage_submitter, tmp_path):
    module, control, args = stage_submitter
    control["runs"] = [{"workflow": module.WORKFLOW, "status": "completed"}]
    argv = render_args(args, tmp_path, "--worker", "colab-auto", "--clips", "3")
    assert module.main(argv) == 0
    inputs = [post["inputs"] for post in control["posts"]]
    assert [i["clip_index"] for i in inputs] == ["1", "2", "3"]
    assert {i["worker"] for i in inputs} == {"colab-auto"}
    assert len({i["request_id"] for i in inputs}) == 3


@pytest.mark.parametrize(
    "extra, active",
    [
        (("--worker", "colab-auto", "--clips", "3"), 2),
        (("--worker", "colab-auto", "--clips", "5"), 0),
        (("--clips", "2"), 0),
        (("--clips", "2", "--clip-index", "1"), 0),
    ],
)
def test_parallel_clips_respect_runtime_limits(
    stage_submitter, tmp_path, extra, active
):
    module, control, args = stage_submitter
    control["runs"] = [{"workflow": module.WORKFLOW, "status": "in_progress"}] * active
    with pytest.raises(RuntimeError):
        module.main(render_args(args, tmp_path, *extra))
    assert not control["posts"]


def test_merge_needs_clip_runs_and_runs_on_the_hosted_runner(stage_submitter):
    module, control, args = stage_submitter
    merge = ["--stage", "merge"] + args[2:]
    with pytest.raises(RuntimeError, match="part-runs"):
        module.main(merge)
    parts = "11:" + "c" * 32 + ",12:" + "d" * 32
    assert module.main(merge + ["--part-runs", parts]) == 0
    assert control["posts"][0]["inputs"]["part_runs"] == parts
    assert control["posts"][0]["inputs"]["worker"] == "cpu"


@pytest.fixture
def local_merge_env(collector, tmp_path, monkeypatch):
    from clipforge import worker_audio as worker

    module = collector[0]
    sources, runs = {}, {}
    for index in (1, 2, 3):
        request = str(index) * 32
        root = tmp_path / f"part-{index}"
        clip = root / f"delivery/clips/clip-{index:03}"
        clip.mkdir(parents=True)
        for name in ("video.mp4", "captions.ass", "captions.srt"):
            (clip / name).write_bytes(b"synthetic clip")
        quality = {"passed": True, "failures": [], "warnings": [], "metrics": {}}
        (clip / "quality.json").write_text(json.dumps(quality))
        (clip / "ledger.json").write_text("[]")
        row = {
            "directory": f"delivery/clips/clip-{index:03}",
            "key": "synthetic-render-key",
            "ledger": [],
            "quality": quality,
            "media": {"width": 1920, "height": 1080, "audio_sample_rate": 48000},
            "sha256": {path.name: sha(path) for path in clip.iterdir()},
        }
        manifest = {
            "status": "clip_rendered",
            "audio_stage": "render",
            "clip_index": index,
            "clip_count": 3,
            "rendered": [row],
            "timings": {f"clip-{index:03}": {"render": 1.0}, "total": 1.0},
            "analysis_key": "e" * 64,
            "selection_sha256": "f" * 64,
            "source_url": KICK_URL,
            "source_sha256": "0" * 64,
            "model_generation_requests": 0,
        }
        (root / "manifest.json").write_text(json.dumps(manifest))
        (root / "delivery/manifest.json").write_text(json.dumps(manifest))
        (root / "asr-receipt.json").write_text(
            json.dumps({"key": ASR_KEY, "reused": True})
        )
        worker.write_receipt(
            root,
            manifest,
            {"previous_request_id": PREVIOUS_REQUEST},
            request,
            SOURCE_REF,
            "render",
            str(100 + index),
            index,
        )
        sources[request] = root
        runs[str(100 + index)] = request
    control = {"run_fault": {}, "calls": []}

    def gh(*args):
        control["calls"].append(args)
        if args[:2] == ("run", "view"):
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + runs[args[2]],
                    "event": "workflow_dispatch",
                    "workflowName": "ClipForge audio-first stages",
                    "headBranch": "main",
                    **control["run_fault"],
                }
            )
        assert args[:2] == ("run", "download")
        request = args[args.index("--name") + 1].removeprefix("clipforge-result-")
        shutil.copytree(sources[request], Path(args[args.index("--dir") + 1]))
        return ""

    monkeypatch.setattr(worker, "gh", gh)
    monkeypatch.setattr(
        module, "gh", lambda *a, **k: pytest.fail("local merge dispatched")
    )
    monkeypatch.setattr(
        worker.landscape.fonts,
        "resolve",
        lambda *a: pytest.fail("merge resolved a font"),
    )
    original_run = subprocess.run

    def only_git(command, *args, **kwargs):
        assert command[0] == "git", "merge invoked an external media tool"
        if command == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout=SOURCE_REF + "\n")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", only_git)
    text = ",".join(f"{run}:{request}" for run, request in runs.items())
    args = ["--stage", "merge", "--local", "--part-runs", text]
    return module, worker, control, sources, args


def test_local_merge_matches_hosted_merge_and_collect_verification(
    local_merge_env, tmp_path, monkeypatch
):
    module, worker, control, sources, args = local_merge_env
    output = tmp_path / "local"
    assert module.main([*args, "--output", str(output)]) == 0
    assert len([c for c in control["calls"] if c[:2] == ("run", "download")]) == 3
    assert (output / "delivery/e2e-summary.json").is_file()
    summary = json.loads((output / "delivery/e2e-summary.json").read_text())
    assert summary["reproducibility"]["git_head"] == SOURCE_REF
    receipt = json.loads((output / "request-receipt.json").read_text())
    assert receipt["workflow_run"] == "local"
    request = {
        "audio_stage": "merge",
        "source_ref": SOURCE_REF,
        "previous_request_id": PREVIOUS_REQUEST,
        "part_runs": args[-1],
    }
    assert module.verify_stage_artifact(
        output, receipt["request_id"], "merge", request, "local"
    ) == len(receipt["sha256"])
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    hosted = tmp_path / "hosted"
    hosted_args = [
        "--stage",
        "merge",
        "--request-id",
        receipt["request_id"],
        "--source-ref",
        SOURCE_REF,
        "--output",
        str(hosted),
    ]
    for source in sources.values():
        hosted_args += ["--part-dir", str(source)]
    assert worker.main(hosted_args) == 0
    assert json.loads((hosted / "manifest.json").read_text()) == json.loads(
        (output / "manifest.json").read_text()
    )
    assert set(module.staged_files(hosted)) == set(module.staged_files(output))


@pytest.mark.parametrize(
    "fault",
    [
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"displayTitle": "ClipForge " + "9" * 32},
    ],
)
def test_local_merge_stops_before_download_when_run_verification_fails(
    local_merge_env, tmp_path, fault
):
    module, _, control, _, args = local_merge_env
    control["run_fault"] = fault
    output = tmp_path / "local"
    with pytest.raises(RuntimeError, match="did not complete successfully"):
        module.main([*args, "--output", str(output)])
    assert not output.exists()
    assert not any(c[:2] == ("run", "download") for c in control["calls"])


def test_local_merge_does_not_publish_output_that_fails_collect_verification(
    local_merge_env, tmp_path, monkeypatch
):
    module, worker, _, _, args = local_merge_env
    original = worker.write_receipt

    def tamper(output, *args, **kwargs):
        original(output, *args, **kwargs)
        (output / "delivery/clips/clip-001/video.mp4").write_bytes(b"changed")

    monkeypatch.setattr(worker, "write_receipt", tamper)
    output = tmp_path / "local"
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        module.main([*args, "--output", str(output)])
    assert not output.exists()


def test_local_merge_reads_source_config_and_rejects_different_checkout(
    local_merge_env, tmp_path, monkeypatch
):
    module, worker, _, _, args = local_merge_env
    calls = []

    def git(command, **kwargs):
        calls.append(command)
        if command == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="9" * 40)
        assert command == ["git", "show", SOURCE_REF + ":config.landscape.example.json"]
        return subprocess.CompletedProcess(
            command, 0, stdout=Path("config.landscape.example.json").read_text()
        )

    monkeypatch.setattr(subprocess, "run", git)
    monkeypatch.setattr(
        worker, "merge_parts", lambda *a: pytest.fail("merged with different code")
    )
    output = tmp_path / "local"
    with pytest.raises(RuntimeError, match="code differs from source_ref"):
        module.main([*args, "--output", str(output)])
    assert len(calls) == 2
    assert not output.exists()


def test_local_merge_rejects_imported_code_from_different_revision(
    local_merge_env, tmp_path, monkeypatch
):
    module, worker, _, _, args = local_merge_env
    monkeypatch.setattr(
        worker.landscape, "code_identity", lambda: {"git_head": "9" * 40}
    )
    output = tmp_path / "local"
    with pytest.raises(RuntimeError, match="code differs from source_ref"):
        module.main([*args, "--output", str(output)])
    assert not output.exists()


def test_local_merge_compares_part_count_with_configuration_plan(
    local_merge_env, tmp_path
):
    module, worker, _, sources, args = local_merge_env
    for root in sources.values():
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["clip_count"] = 4
        path.write_text(json.dumps(manifest))
        receipt_path = root / "request-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["sha256"]["manifest.json"] = worker.digest(path)
        receipt_path.write_text(json.dumps(receipt))
    output = tmp_path / "local"
    with pytest.raises(RuntimeError, match="clip count does not match the plan"):
        module.main([*args, "--output", str(output)])
    assert not output.exists()


def test_audio_stages_yaml_loads_and_merge_skips_os_packages():
    import yaml

    workflow = yaml.safe_load(Path("deploy/audio-stages.yml").read_text())
    steps = workflow["jobs"]["audio-stage"]["steps"]
    cache = next(step for step in steps if step.get("uses") == "actions/cache@v4")
    assert cache["if"] == "inputs.audio_stage != 'merge'"
    install = next(step for step in steps if "dpkg-query" in step.get("run", ""))
    assert install["if"] == cache["if"]
    assert "apt-get install --no-download" in install["run"]


@pytest.mark.parametrize("path", sorted(Path("deploy").glob("*.yml")))
def test_deployment_yaml_loads(path):
    import yaml

    workflow = yaml.safe_load(path.read_text())
    assert workflow[True]["workflow_dispatch"] is not None
    assert workflow["jobs"]


@pytest.mark.parametrize("worker", ["cpu", "colab-auto"])
@pytest.mark.parametrize("stage", ["previews", "render", "merge"])
def test_audio_stage_workflow_validates_worker_stage_combination(
    tmp_path, monkeypatch, worker, stage
):
    import yaml

    workflow = yaml.safe_load(Path("deploy/audio-stages.yml").read_text())
    job = workflow["jobs"]["colab-render" if worker == "colab-auto" else "audio-stage"]
    assert f"inputs.worker == '{worker}'" in job["if"]
    assert "inputs.audio_stage" not in job["if"]
    for name, value in {
        "SOURCE_REF": SOURCE_REF,
        "CLIPFORGE_SOURCE_REF": SOURCE_REF,
        "REQUEST_ID": "d" * 32,
        "INPUT_TAG": "clipforge-input-" + "d" * 32,
        "PREVIOUS_RUN": str(PREVIOUS_RUN),
        "PREVIOUS_REQUEST": PREVIOUS_REQUEST,
        "AUDIO_STAGE": stage,
        "CLIP_INDEX": "",
        "PART_RUNS": "123:" + "c" * 32 if stage == "merge" else "",
        "REVIEW_JSON": '{"clips": [{"video_reviewed": true}]}',
        "CONFIG_FILE": "config.landscape.example.json",
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    code = job["steps"][0]["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    if worker == "colab-auto" and stage != "render":
        with pytest.raises(
            SystemExit, match="worker=colab-auto requires audio_stage=render"
        ):
            exec(compile(code, "stage-validation", "exec"), {})
        assert not (tmp_path / "review.json").exists()
    else:
        exec(compile(code, "stage-validation", "exec"), {})
        assert json.loads((tmp_path / "review.json").read_text())["clips"]


def test_video_workflow_uses_only_hosted_cpu_or_automatic_colab():
    import yaml

    workflow = yaml.safe_load(Path("deploy/clipforge.yml").read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["worker"]["options"] == ["colab-auto", "cpu"]
    assert inputs["worker"]["default"] == "colab-auto"
    for name, worker in (("clip", "cpu"), ("colab-auto", "colab-auto")):
        job = workflow["jobs"][name]
        assert job["runs-on"] == "ubuntu-24.04"
        assert f"inputs.worker == '{worker}'" in job["if"]
    assert workflow["jobs"]["clip"]["env"]["CLIPFORGE_REQUIRE_CUDA"] == "0"


CHAT_CONFIG = "config.landscape-chat.example.json"


@pytest.mark.parametrize(
    "name",
    [
        "../config.landscape.example.json",
        "config/landscape.example.json",
        "config.Landscape.example.json",
        "config.landscape.json",
        "config.landscape.example.json.bak",
        "config.a_b.example.json",
        "config..example.json",
    ],
)
def test_config_file_name_is_limited_before_any_dispatch(
    stage_submitter, tmp_path, name
):
    module, control, args = stage_submitter
    with pytest.raises(RuntimeError, match="name is not allowed"):
        module.main([*render_args(args, tmp_path), "--config-file", name])
    assert not control["posts"]


def test_config_file_must_be_committed_at_the_source_ref(stage_submitter, tmp_path):
    module, control, args = stage_submitter
    control["configs"].discard(CHAT_CONFIG)
    with pytest.raises(RuntimeError, match="not committed at --source-ref"):
        module.main([*render_args(args, tmp_path), "--config-file", CHAT_CONFIG])
    assert not control["posts"]


def test_config_file_check_reads_the_real_commit(stage_submitter):
    module, control, _ = stage_submitter
    real = control["real_config_in_source"]
    assert real("HEAD", "config.landscape.example.json")
    assert not real("HEAD", "config.no-such-setting.example.json")
    assert not real("0" * 40, "config.landscape.example.json")


def test_config_file_is_part_of_the_request_identity_and_receipt(
    stage_submitter, tmp_path
):
    module, control, args = stage_submitter
    render = render_args(args, tmp_path)
    assert module.main(render) == 0
    assert module.main([*render, "--config-file", CHAT_CONFIG]) == 0
    default_inputs, chat_inputs = (post["inputs"] for post in control["posts"])
    assert default_inputs["config_file"] == "config.landscape.example.json"
    assert chat_inputs["config_file"] == CHAT_CONFIG
    assert default_inputs["request_id"] != chat_inputs["request_id"]
    receipt_path = tmp_path / ".local/state/clipforge" / chat_inputs["request_id"]
    receipt = json.loads((receipt_path / "receipt.json").read_text())
    assert receipt["config_file"] == CHAT_CONFIG
    assert json.loads((receipt_path / "request.json").read_text())["config_file"] == (
        CHAT_CONFIG
    )


def test_config_file_applies_only_to_render(stage_submitter):
    module, control, args = stage_submitter
    with pytest.raises(RuntimeError, match="render only"):
        module.main([*args, "--config-file", CHAT_CONFIG])
    assert not control["posts"]


def write_part_receipt(state, request_id, config_file=None):
    """A local dispatch receipt; receipts from before the option carry no config_file."""
    path = state / request_id / "receipt.json"
    path.parent.mkdir(parents=True)
    value = {"request_id": request_id}
    if config_file is not None:
        value["config_file"] = config_file
    path.write_text(json.dumps(value))


def test_merge_inherits_the_configuration_its_parts_used(stage_submitter, tmp_path):
    module, control, args = stage_submitter
    state = tmp_path / ".local/state/clipforge"
    write_part_receipt(state, "c" * 32, CHAT_CONFIG)
    write_part_receipt(state, "d" * 32, CHAT_CONFIG)
    parts = "11:" + "c" * 32 + ",12:" + "d" * 32
    merge = ["--stage", "merge"] + args[2:] + ["--part-runs", parts]
    assert module.main(merge) == 0
    assert control["posts"][0]["inputs"]["config_file"] == CHAT_CONFIG


def test_merge_refuses_mixed_part_configurations_before_dispatch(
    stage_submitter, tmp_path
):
    module, control, args = stage_submitter
    state = tmp_path / ".local/state/clipforge"
    write_part_receipt(state, "c" * 32, CHAT_CONFIG)
    write_part_receipt(state, "d" * 32)
    parts = "11:" + "c" * 32 + ",12:" + "d" * 32
    merge = ["--stage", "merge"] + args[2:] + ["--part-runs", parts]
    with pytest.raises(RuntimeError, match="different configuration files"):
        module.main(merge)
    assert not control["posts"]


def test_local_merge_refuses_mixed_part_configurations(local_merge_env, tmp_path):
    module, worker, control, sources, args = local_merge_env
    path = sources["2" * 32] / "request-receipt.json"
    receipt = json.loads(path.read_text())
    receipt["config_file"] = CHAT_CONFIG
    path.write_text(json.dumps(receipt))
    output = tmp_path / "local"
    with pytest.raises(RuntimeError, match="different configuration files"):
        module.main([*args, "--output", str(output)])
    assert not output.exists()


def test_local_merge_reads_the_configuration_its_parts_used(
    local_merge_env, tmp_path, monkeypatch
):
    module, worker, _, sources, args = local_merge_env
    for root in sources.values():
        path = root / "request-receipt.json"
        receipt = json.loads(path.read_text())
        receipt["config_file"] = CHAT_CONFIG
        path.write_text(json.dumps(receipt))
    calls = []

    def git(command, **kwargs):
        calls.append(command)
        if command == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="9" * 40)
        assert command == ["git", "show", SOURCE_REF + ":" + CHAT_CONFIG]
        return subprocess.CompletedProcess(
            command, 0, stdout=Path("config.landscape.example.json").read_text()
        )

    monkeypatch.setattr(subprocess, "run", git)
    monkeypatch.setattr(
        worker, "merge_parts", lambda *a, **k: pytest.fail("merged with different code")
    )
    with pytest.raises(RuntimeError, match="code differs from source_ref"):
        module.main([*args, "--output", str(tmp_path / "local")])
    assert len(calls) == 2


def test_audio_stage_workflow_passes_a_validated_config_file_to_both_paths():
    import yaml

    workflow = yaml.safe_load(Path("deploy/audio-stages.yml").read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["config_file"]["default"] == "config.landscape.example.json"
    for name in ("audio-stage", "colab-render"):
        job = workflow["jobs"][name]
        assert job["env"]["CONFIG_FILE"] == "${{ inputs.config_file }}"
        assert "os.environ['CONFIG_FILE']" in job["steps"][0]["run"]
    steps = workflow["jobs"]["audio-stage"]["steps"]
    run = next(
        step["run"]
        for step in steps
        if step.get("name") == "Reuse verified ASR and execute only the requested stage"
    )
    assert run.count('--config "$CONFIG_FILE"') == 2


@pytest.mark.parametrize(
    "name",
    [
        "../config.landscape.example.json",
        "config.landscape.json",
        "config.a.example.json;rm",
    ],
)
def test_audio_stage_workflow_rejects_unlisted_config_files(
    tmp_path, monkeypatch, name
):
    import yaml

    workflow = yaml.safe_load(Path("deploy/audio-stages.yml").read_text())
    code = (
        workflow["jobs"]["audio-stage"]["steps"][0]["run"]
        .split("python3 - <<'PY'\n", 1)[1]
        .rsplit("\nPY", 1)[0]
    )
    for key, value in {
        "SOURCE_REF": SOURCE_REF,
        "REQUEST_ID": "d" * 32,
        "INPUT_TAG": "clipforge-input-" + "d" * 32,
        "PREVIOUS_RUN": str(PREVIOUS_RUN),
        "PREVIOUS_REQUEST": PREVIOUS_REQUEST,
        "AUDIO_STAGE": "render",
        "CLIP_INDEX": "",
        "PART_RUNS": "",
        "CONFIG_FILE": name,
        "REVIEW_JSON": '{"clips": [{"video_reviewed": true}]}',
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(AssertionError):
        exec(compile(code, "stage-validation", "exec"), {})


def test_review_refuses_an_unknown_crop_side_before_dispatch():
    spec = importlib.util.spec_from_file_location(
        "submit_audio_stage", Path("tools/submit_audio_stage.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.require_video_review(
        {
            "clips": [
                {"video_reviewed": True, "crop_anchor": "left"},
                {"video_reviewed": True},
            ]
        }
    )
    with pytest.raises(RuntimeError, match="crop_anchor"):
        module.require_video_review(
            {"clips": [{"video_reviewed": True, "crop_anchor": "middle"}]}
        )


def test_command_failure_explains_local_code_mismatch_without_external_details(
    stage_submitter,
):
    submit, _, _ = stage_submitter

    reason = "local merge code differs from source_ref"
    assert reason in submit.command_failure(submit.AudioStageError(reason))
    hidden = "https://private.example/input?token=SECRET_AUTH_VALUE"
    assert hidden not in submit.command_failure(RuntimeError(hidden))
    assert "SECRET_AUTH_VALUE" not in submit.command_failure(ValueError(hidden))


def test_review_refuses_a_caption_floor_out_of_range_before_dispatch():
    spec = importlib.util.spec_from_file_location(
        "submit_audio_stage", Path("tools/submit_audio_stage.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.require_video_review(
        {
            "clips": [
                {"video_reviewed": True, "caption_floor": 0.32},
                {"video_reviewed": True},
            ]
        }
    )
    for floor in (0.4, -0.01):
        with pytest.raises(RuntimeError, match="caption_floor"):
            module.require_video_review(
                {"clips": [{"video_reviewed": True, "caption_floor": floor}]}
            )
