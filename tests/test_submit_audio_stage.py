import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest


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
        "timeout": False,
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
            return json.dumps([{"status": row["status"]} for row in rows])
        control["posts"].append(json.loads(body))
        if control["timeout"]:
            raise TimeoutError("reply lost")
        return ""

    monkeypatch.setattr(module, "gh", gh)
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
    assert payload["ref"] == "ccr-12fe9485-ugaszt"
    assert payload["inputs"]["previous_request_id"] == "b" * 32
    with pytest.raises(RuntimeError, match="already has a request"):
        module.main(args)
    assert len(control["posts"]) == 1


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
        "clipforge.yml"
    )


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


def test_collect_accepts_legacy_receipt_without_previous_keys(collector, tmp_path):
    module, control, _ = collector
    legacy = tmp_path / "legacy"
    build_artifact(legacy, drop=("previous_run_id", "previous_request_id"))
    control["artifact"] = legacy
    destination = tmp_path / "collected"
    assert module.main(collect_args(destination)) == 0
    assert (destination / "previews/topic-001.mp4").is_file()
    receipt = json.loads((destination / "request-receipt.json").read_text())
    assert "previous_run_id" not in receipt


def test_collect_requires_a_successful_stage_run(collector, tmp_path):
    module, control, _ = collector
    control["run"]["conclusion"] = "failure"
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="did not complete successfully"):
        module.main(collect_args(destination))
    assert control["downloads"] == 0 and not destination.exists()


def render_args(args, tmp_path, *extra):
    review = tmp_path / "review.json"
    review.write_text('{"clips": []}')
    return [
        *(["--stage", "render"] + args[2:]),
        "--review-file",
        str(review),
        *extra,
    ]


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
