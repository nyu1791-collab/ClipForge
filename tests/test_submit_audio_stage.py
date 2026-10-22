import importlib.util
import json
from pathlib import Path

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
