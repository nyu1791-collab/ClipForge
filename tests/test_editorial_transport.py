import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("colab_actions"), importlib.import_module(
        "colab_remote_job"
    )


def test_only_valid_private_review_tags_allow_larger_subtitle_metadata(
    tmp_path, monkeypatch, transport
):
    control, _ = transport
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"clips": [], "padding": "x" * 60000}))
    for name, value in {
        "AUDIO_STAGE": "render",
        "PREVIOUS_RUN": "123",
        "PREVIOUS_REQUEST": "a" * 32,
        "REVIEW_FILE": str(path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLIPFORGE_AUDIO_REVIEW_RELEASE", raising=False)
    with pytest.raises(control.ControlError, match="too large"):
        control.stage_request()
    monkeypatch.setenv("CLIPFORGE_AUDIO_REVIEW_RELEASE", "clipforge-kick-editorial-123")
    assert len(control.stage_request()["review"]) > 60000
    monkeypatch.setenv("CLIPFORGE_AUDIO_REVIEW_RELEASE", "../another-release")
    with pytest.raises(control.ControlError, match="Invalid audio review"):
        control.stage_request()


@pytest.mark.parametrize(
    "fault",
    [None, "different_selection", "incomplete", "invalid_tag", "audio_understanding"],
)
def test_gpu_stage_requires_the_completed_private_selection_to_match(
    tmp_path, monkeypatch, transport, fault
):
    _, remote = transport
    monkeypatch.setattr(remote, "ROOT", tmp_path)
    monkeypatch.setattr(remote, "WORK", tmp_path)
    monkeypatch.setattr(remote, "read_setup_timings", lambda path: {})
    output = tmp_path / "out"
    output.mkdir()
    expected = {"clips": [{"speaker_turns": []}]}
    if fault == "audio_understanding":
        expected["selection_basis"] = "audio_understanding"
    calls = []

    class Bundles:
        def json_request(self, method, url):
            calls.append("release")
            return {
                "assets": [
                    {"name": "audio-editor-receipt.json"},
                    {"name": "final-selection.json"},
                    {"name": "selected-transcripts.json"},
                ]
            }

        def download_asset(self, asset, path, bound):
            if asset["name"] == "audio-editor-receipt.json":
                value = {"status": "failed" if fault == "incomplete" else "complete"}
            else:
                value = {"clips": []} if fault == "different_selection" else expected
            path.write_text(json.dumps(value))

    monkeypatch.setattr(remote, "ReleaseBundles", lambda token: Bundles())

    def command(arguments, environment, name):
        calls.append(name)
        if name == "render":
            (output / "manifest.json").write_text(json.dumps({"status": "complete"}))

    monkeypatch.setattr(remote, "command", command)
    payload = {
        "github_token": "synthetic",
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "workflow_run": "123",
        "audio_review_release": "../invalid"
        if fault == "invalid_tag"
        else "clipforge-kick-editorial-replay-123",
        "stage": {
            "previous_run": "123",
            "previous_request": "c" * 32,
            "review": json.dumps(expected),
        },
    }
    if fault and fault != "audio_understanding":
        with pytest.raises(RuntimeError):
            remote.render_stage(payload, output, {}, {}, {})
        assert "render" not in calls
        if fault == "invalid_tag":
            assert "release" not in calls
    else:
        assert remote.render_stage(payload, output, {}, {}, {}) == "complete"
        assert calls == ["setup", "release", "render"]

        if fault == "audio_understanding":
            receipt = json.loads((output / "request-receipt.json").read_text())
            assert receipt["selection_basis"] == "audio_understanding"
