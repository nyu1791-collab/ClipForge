"""Network-free boundary checks for URL intake and native agent events."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from url_common import youtube_url, video_profile
from opencode_session import consume
from agent_runner import configuration
from clipforge.models import Settings


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/dQw4w9WgXcQ?t=3",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=other",
        "https://youtube.com/live/dQw4w9WgXcQ",
    ],
)
def test_public_url_normalization(url):
    assert youtube_url(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
        "https://name:password@youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/playlist?list=secret",
        "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
        "https://youtube.com:8080/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&v=aaaaaaaaaaa",
    ],
)
def test_url_rejections(url):
    with pytest.raises(ValueError):
        youtube_url(url)


@pytest.mark.parametrize("duration", [24, 36, 120, 480, 3600])
def test_profile_stays_within_p1(duration):
    settings = Settings.model_validate(video_profile({"duration": duration}))
    assert settings.candidates * settings.max_seconds <= duration * 0.5
    assert settings.candidates * settings.max_seconds <= 180
    assert settings.gemini_model == "gemini-3.8-flash"


@pytest.mark.parametrize(
    "metadata",
    [
        {"duration": 3601},
        {"duration": 23},
        {"duration": float("nan")},
        {"duration": True},
        {"duration": 60, "is_live": True},
        {"duration": 60, "live_status": "post_live"},
        {"duration": 60, "availability": "premium_only"},
    ],
)
def test_video_policy(metadata):
    with pytest.raises(ValueError):
        video_profile(metadata)


def event(kind, **properties):
    return b"data:" + json.dumps({"type": kind, "properties": properties}).encode()


@pytest.mark.parametrize(
    "kind,properties",
    [
        ("session.status", {"status": {"type": "retry"}}),
        ("permission.asked", {}),
        ("session.error", {}),
        (
            "message.updated",
            {
                "info": {
                    "sessionID": "s",
                    "role": "assistant",
                    "providerID": "another",
                    "modelID": "paid",
                }
            },
        ),
    ],
)
def test_retry_permission_error_and_model_drift_stop(kind, properties):
    with pytest.raises(RuntimeError):
        consume([event(kind, sessionID="s", **properties)], "s")


def test_complete_session_deduplicates_streamed_tools():
    info = {
        "id": "a",
        "sessionID": "s",
        "role": "assistant",
        "providerID": "opencode",
        "modelID": "deepseek-v4.1-flash-free",
    }

    def part(id, type, **data):
        return event(
            "message.part.updated",
            part={"id": id, "type": type, "messageID": "a", "sessionID": "s", **data},
        )

    events = [
        event("message.updated", info=info),
        part("start", "step-start"),
        part("start", "step-start"),
        part("t", "tool", tool="edit", state={"status": "running"}),
        part("t", "tool", tool="edit", state={"status": "completed"}),
        part("answer", "text", text="done"),
        part("end", "step-finish", reason="stop"),
        event("session.status", sessionID="s", status={"type": "idle"}),
    ]
    tools, answer, steps = consume(events, "s")
    assert tools == [{"tool": "edit", "status": "completed"}]
    assert answer == "done" and steps == 1


def test_agent_policy_has_no_publish_or_unlimited_shell(tmp_path):
    config = configuration(tmp_path, fixture=True)
    assert config["permission"]["*"] == "deny"
    assert config["permission"]["shell"]["*"] == "deny"
    assert "git diff*" not in config["permission"]["shell"]
    assert config["permission"]["external_directory"] == "deny"
    assert config["enabled_providers"] == ["opencode"]
    assert config["agent"]["title"]["disable"]


def test_queue_dispatch_is_not_repeated_after_ambiguous_failure(tmp_path, monkeypatch):
    import url_bridge as bridge

    monkeypatch.setattr(bridge, "STATE", tmp_path)
    config = tmp_path / "config"
    config.write_text(
        json.dumps({"allowed_uids": [1000, 1001], "source_ref": "a" * 40})
    )
    monkeypatch.setattr(bridge, "CONFIG", config)
    calls = []

    def ambiguous(args, timeout=90):
        calls.append(args)
        if args[:2] == ["workflow", "run"]:
            raise RuntimeError("connection interrupted")
        return b"[]"

    monkeypatch.setattr(bridge, "gh", ambiguous)
    job = bridge.handle({"op": "submit", "url": "https://youtu.be/dQw4w9WgXcQ"}, 1001)
    bridge.tick()
    bridge.tick()
    assert sum(c[:2] == ["workflow", "run"] for c in calls) == 1
    assert bridge.load(job["id"])["status"] == "dispatching"
    with pytest.raises(PermissionError):
        bridge.handle({"op": "fixture"}, 65534)


def test_unknown_dispatch_blocks_new_generation(tmp_path, monkeypatch):
    import url_bridge as bridge

    monkeypatch.setattr(bridge, "STATE", tmp_path)
    bridge.save({"id": "a" * 32, "created_at": 1, "status": "needs_reconciliation"})
    bridge.save({"id": "b" * 32, "created_at": 2, "status": "queued"})

    def forbidden(*args, **kwargs):
        pytest.fail("must not dispatch")

    monkeypatch.setattr(bridge, "gh", forbidden)
    bridge.tick()
    assert bridge.load("b" * 32)["status"] == "queued"


def test_download_is_cookie_free_and_has_zero_retries(tmp_path):
    from url_runner import download_command

    args = download_command("https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)
    for flag in ("--retries", "--fragment-retries", "--extractor-retries"):
        assert args[args.index(flag) + 1] == "0"
    assert "--cookies" not in args
    assert "--ignore-config" in args and "--no-playlist" in args


def test_agent_validation_subprocess_does_not_inherit_credentials(
    tmp_path, monkeypatch
):
    from agent_runner import command

    monkeypatch.setenv("OPENCODE_API_KEY", "sentinel-value")
    monkeypatch.setenv("GITHUB_TOKEN", "sentinel-gh")
    code = "import os; assert 'OPENCODE_API_KEY' not in os.environ; assert 'GITHUB_TOKEN' not in os.environ"
    command([sys.executable, "-c", code], tmp_path)
