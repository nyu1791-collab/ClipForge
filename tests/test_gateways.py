import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from clipforge import pipeline
from clipforge.models import Settings


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location("test_tool_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def submitter(tmp_path, monkeypatch):
    module = load_tool("submit_job")
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-video" * 300)
    control = {
        "private": True,
        "active": False,
        "request": None,
        "posts": [],
        "releases": [],
        "tamper": False,
    }

    def gh(*args, body=None, timeout=None):
        if args[:2] == ("api", "user"):
            return "nyu1791-collab"
        if args[:2] == ("repo", "view"):
            return json.dumps({"isPrivate": control["private"]})
        if args[:2] == ("secret", "list"):
            return json.dumps([{"name": "GEMINI_API_KEY"}])
        if args[0] == "api" and "/commits/" in args[1]:
            return json.dumps({"sha": "a" * 40})
        if args[0] == "api" and "actions/workflows/" in args[1]:
            return json.dumps({"state": "active"})
        if args[0] == "api" and "--method" in args:
            control["posts"].append(json.loads(body))
            return ""
        if args[:2] == ("run", "list"):
            if control["active"]:
                return json.dumps([{"status": "in_progress"}])
            if not control["posts"]:
                return "[]"
            return json.dumps(
                [
                    {
                        "databaseId": 99,
                        "displayTitle": "ClipForge " + control["request"]["request_id"],
                        "event": "workflow_dispatch",
                        "status": "completed",
                    }
                ]
            )
        if args[:2] == ("run", "view"):
            return json.dumps({"status": "completed", "conclusion": "success"})
        if args[:2] == ("release", "create"):
            request = next(Path(a) for a in args if a.endswith("/request.json"))
            control["request"] = json.loads(request.read_text())
            control["releases"].append(args[2])
            assert Path(args[3]).read_bytes() == source.read_bytes()
            return ""
        if args[:2] == ("release", "delete"):
            control["releases"].append("deleted")
            return ""
        if args[:2] == ("run", "download"):
            destination = Path(args[args.index("--dir") + 1])
            destination.mkdir(parents=True)
            directory = destination / "clips/clip-001"
            directory.mkdir(parents=True)
            hashes = {}
            for name in ["video.mp4", "captions.ass", "captions.srt"]:
                data = ("synthetic-" + name).encode()
                (directory / name).write_bytes(data)
                hashes[name] = hashlib.sha256(data).hexdigest()
            request = control["request"]
            (destination / "request-receipt.json").write_text(
                json.dumps(
                    {
                        "request_id": request["request_id"],
                        "source_ref": request["source_ref"],
                        "status": "complete",
                        "rendered_clips": 1,
                    }
                )
            )
            (destination / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "source_sha256": request["video_sha256"],
                        "rendered": [{"directory": "clips/clip-001", "sha256": hashes}],
                    }
                )
            )
            if control["tamper"]:
                (directory / "video.mp4").write_bytes(b"corrupted")
            return ""
        raise AssertionError("unexpected mocked gh command: " + str(args))

    monkeypatch.setattr(module, "gh", gh)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    def invoke(*extra):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "submit_job",
                "--input",
                str(source),
                "--gemini-model",
                "test-video-model",
                *extra,
            ],
        )
        module.main()

    return module, source, control, invoke


def test_dispatch_is_private_pinned_and_correlated(submitter, tmp_path):
    module, source, control, invoke = submitter
    original = source.read_bytes()
    config = tmp_path / "config.json"
    config.write_text('{"clips":2}')
    invoke("--config", str(config))
    request = control["request"]
    assert request["settings"]["clips"] == 2
    assert request["source_ref"] == "a" * 40
    assert request["video_sha256"] == hashlib.sha256(original).hexdigest()
    assert control["posts"][0]["inputs"]["request_id"] == request["request_id"]
    assert len(control["posts"]) == len(control["releases"]) == 1
    assert source.read_bytes() == original
    receipt = (
        tmp_path / ".local/state/clipforge" / request["request_id"] / "receipt.json"
    )
    assert json.loads(receipt.read_text())["run_id"] == 99
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("condition", ["private", "active"])
def test_rejects_public_repository_and_active_jobs(submitter, condition):
    _, _, control, invoke = submitter
    control[condition] = False if condition == "private" else True
    with pytest.raises(RuntimeError):
        invoke()
    assert not control["posts"] and not control["releases"]


@pytest.mark.parametrize("model", ["bad/model", "with space", "line\nbreak"])
def test_invalid_model_never_uploads(submitter, model):
    _, _, control, invoke = submitter
    with pytest.raises(RuntimeError):
        invoke("--gemini-model", model)
    assert not control["posts"] and not control["releases"]


def test_wait_download_verifies_output(submitter, tmp_path):
    _, _, control, invoke = submitter
    destination = tmp_path / "result"
    invoke("--wait", "--output", str(destination))
    assert (destination / "clips/clip-001/video.mp4").is_file()
    receipt = (
        tmp_path
        / ".local/state/clipforge"
        / control["request"]["request_id"]
        / "receipt.json"
    )
    assert json.loads(receipt.read_text())["status"] == "COMPLETE"


def test_failed_download_does_not_leave_partial_destination(submitter, tmp_path):
    _, _, control, invoke = submitter
    control["tamper"] = True
    destination = tmp_path / "result"
    with pytest.raises(RuntimeError, match="checksum"):
        invoke("--wait", "--output", str(destination))
    assert not destination.exists()


def test_gateway_settings_field_list_tracks_model(submitter):
    assert submitter[0].SETTINGS_FIELDS == set(Settings.model_fields)


def test_configuration_cannot_upload_credentials(submitter, tmp_path):
    _, _, control, invoke = submitter
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"GEMINI_API_KEY": "synthetic-not-a-real-key"}))
    with pytest.raises(RuntimeError, match="unknown fields"):
        invoke("--config", str(config))
    assert not control["releases"] and not control["posts"]


def test_recover_download_does_not_submit_again(submitter, tmp_path, monkeypatch):
    module, _, control, invoke = submitter
    invoke()
    identity = control["request"]["request_id"]
    monkeypatch.setattr(
        sys,
        "argv",
        ["submit_job", "--collect", identity, "--output", str(tmp_path / "recovered")],
    )
    module.main()
    assert len(control["posts"]) == 1
    assert (tmp_path / "recovered/clips/clip-001/video.mp4").is_file()


def test_status_does_not_submit_or_download(submitter, monkeypatch, capsys):
    module, _, control, invoke = submitter
    invoke()
    identity = control["request"]["request_id"]
    monkeypatch.setattr(sys, "argv", ["submit_job", "--status", identity])
    module.main()
    assert len(control["posts"]) == 1
    assert "github_status" in capsys.readouterr().out


def test_download_can_be_retried_without_regenerating(submitter, tmp_path, monkeypatch):
    module, _, control, invoke = submitter
    control["tamper"] = True
    with pytest.raises(RuntimeError):
        invoke("--wait", "--output", str(tmp_path / "bad"))
    control["tamper"] = False
    identity = control["request"]["request_id"]
    monkeypatch.setattr(
        sys,
        "argv",
        ["submit_job", "--collect", identity, "--output", str(tmp_path / "good")],
    )
    module.main()
    assert len(control["posts"]) == 1
    assert (tmp_path / "good/manifest.json").is_file()


def test_cleanup_rejects_unrelated_tags(submitter, monkeypatch):
    module, _, control, _ = submitter
    monkeypatch.setattr(
        sys, "argv", ["submit_job", "--cleanup-tag", "unrelated-release"]
    )
    with pytest.raises(RuntimeError):
        module.main()
    assert not control["releases"]


@pytest.fixture
def runner(tmp_path, monkeypatch, settings):
    module = load_tool("run_request")
    identity = "b" * 32
    tag = "clipforge-input-" + identity
    source = b"synthetic-private-video" * 100
    request = {
        "request_id": identity,
        "source_ref": "a" * 40,
        "video_sha256": hashlib.sha256(source).hexdigest(),
        "video_bytes": len(source),
        "settings": settings.model_dump(),
        "chat_sha256": None,
    }
    control = {"request": request, "private": True, "deleted": False, "source": source}
    monkeypatch.setenv("GITHUB_REPOSITORY", module.PRIVATE_REPO)
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))

    def gh(*args):
        if args[:2] == ("repo", "view"):
            return json.dumps({"isPrivate": control["private"]})
        if args[:2] == ("release", "view"):
            return json.dumps(
                {
                    "assets": [
                        {
                            "name": "request.json",
                            "size": len(json.dumps(control["request"])),
                        },
                        {"name": "input-video.bin", "size": len(source)},
                    ]
                }
            )
        if args[:2] == ("release", "download"):
            destination = Path(args[args.index("--dir") + 1])
            name = args[args.index("--pattern") + 1]
            (destination / name).write_bytes(
                json.dumps(control["request"]).encode()
                if name == "request.json"
                else control["source"]
            )
            return ""
        if args[:2] == ("release", "delete"):
            control["deleted"] = True
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(module, "gh", gh)

    def invoke(command):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_request",
                command,
                "--tag",
                tag,
                "--directory",
                str(tmp_path / "input"),
                "--output",
                str(tmp_path / "output"),
            ],
        )
        module.main()

    return module, control, invoke


def test_runner_validates_input_and_writes_receipt(runner, tmp_path, monkeypatch):
    _, _, invoke = runner
    invoke("prepare")
    assert "cache_key=" in (tmp_path / "github-output").read_text()
    monkeypatch.setattr(
        pipeline, "run", lambda *a, **kw: {"status": "complete", "rendered": [{}]}
    )
    invoke("run")
    result = json.loads((tmp_path / "output/request-receipt.json").read_text())
    assert result["request_id"] == "b" * 32 and result["workflow_run"] == "123"


def test_runner_wrong_checksum_stops_before_pipeline(runner, monkeypatch):
    _, control, invoke = runner
    control["source"] = b"wrong-content"
    invoke("prepare")
    monkeypatch.setattr(
        pipeline, "run", lambda *a, **kw: pytest.fail("pipeline must not execute")
    )
    with pytest.raises(RuntimeError, match="checksum"):
        invoke("run")


@pytest.mark.parametrize(
    "patch",
    [
        {"request_id": "c" * 32},
        {"source_ref": "c" * 40},
        {"video_sha256": "bad"},
        {"video_bytes": 0},
        {"video_bytes": True},
        {"unknown": "field"},
    ],
)
def test_runner_rejects_bad_manifest(runner, patch):
    _, control, invoke = runner
    control["request"].update(patch)
    with pytest.raises(RuntimeError):
        invoke("prepare")


def test_runner_cleanup_only_on_private_repository(runner):
    _, control, invoke = runner
    control["private"] = False
    with pytest.raises(RuntimeError):
        invoke("cleanup")
    assert not control["deleted"]
