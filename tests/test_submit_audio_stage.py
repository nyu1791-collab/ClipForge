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
    control = {"private": True, "active": False, "posts": [], "timeout": False}

    def gh(*args, body=None):
        if args[:2] == ("api", "user"):
            return "nyu1791-collab"
        if args[:2] == ("repo", "view"):
            return json.dumps({"isPrivate": control["private"]})
        if args[:2] == ("run", "list"):
            return json.dumps([{"status": "in_progress"}] if control["active"] else [])
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


@pytest.mark.parametrize(
    "fault", ["active", "public", "missing_review", "invalid_source"]
)
def test_invalid_stage_never_starts_a_worker(stage_submitter, fault):
    module, control, args = stage_submitter
    if fault == "active":
        control["active"] = True
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


def build_artifact(root, generated_sha=None, **receipt_overrides):
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
    }
    receipt.update(receipt_overrides)
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


def test_collect_requires_a_successful_stage_run(collector, tmp_path):
    module, control, _ = collector
    control["run"]["conclusion"] = "failure"
    destination = tmp_path / "collected"
    with pytest.raises(RuntimeError, match="did not complete successfully"):
        module.main(collect_args(destination))
    assert control["downloads"] == 0 and not destination.exists()
