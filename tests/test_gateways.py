import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from clipforge.models import Settings


KICK_URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location("test_tool_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def submitter(tmp_path, monkeypatch):
    from clipforge import kick_media

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
        "calls": [],
        "preflight_duration": 5536,
        "preflight_error": None,
        "preflight_closed": False,
        "preflight_budgets": [],
        "playlists": [],
    }

    class VOD:
        def __init__(self, url, cache, **kwargs):
            control["preflight_budgets"].append(kwargs)
            assert kwargs["max_seconds"] == 86400

        def playlist(self, quality):
            control["playlists"].append(quality)
            if control["preflight_error"]:
                raise control["preflight_error"]
            return [{"start": 0, "end": control["preflight_duration"]}], 160

        def close(self):
            control["preflight_closed"] = True

    monkeypatch.setattr(kick_media, "KickVOD", VOD)

    def gh(*args, body=None, timeout=None):
        control["calls"].append(args)
        if args[:2] == ("api", "user"):
            return "nyu1791-collab"
        if args[:1] == ("api",) and args[1].count("/") == 2 and "--jq" in args:
            return "true" if control["private"] else "false"
        if args[:2] == ("repo", "view"):
            return json.dumps({"isPrivate": control["private"]})
        if args[:2] == ("secret", "list"):
            return json.dumps([{"name": "GEMINI_API_KEY"}, {"name": "COLAB_ADC_JSON"}])
        if args[0] == "api" and "/commits/" in args[1]:
            return json.dumps({"sha": "a" * 40})
        if args[0] == "api" and "actions/workflows/" in args[1]:
            return json.dumps({"state": "active"})
        if args[0] == "api" and "--method" in args:
            control["posts"].append(json.loads(body))
            return ""
        if args[:2] == ("run", "list"):
            others = [
                {
                    "databaseId": 1,
                    "displayTitle": "ClipForge other",
                    "event": "workflow_dispatch",
                    "status": "in_progress",
                }
            ] * int(control["active"])
            if not control["posts"]:
                return json.dumps(others)
            return json.dumps(
                others
                + [
                    {
                        "databaseId": 99,
                        "displayTitle": "ClipForge "
                        + control["posts"][0]["inputs"]["request_id"],
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
            if control["request"]["video_sha256"] is not None:
                assert Path(args[3]).read_bytes() == source.read_bytes()
            else:
                assert args[3] == str(request)
            return ""
        if args[:2] == ("release", "delete"):
            control["releases"].append("deleted")
            return ""
        if args[:2] == ("run", "download"):
            destination = Path(args[args.index("--dir") + 1])
            destination.mkdir(parents=True)
            inputs = control["posts"][0]["inputs"]
            if inputs["audio_url"]:
                (destination / "transcripts").mkdir()
                key = "e" * 64
                audio = destination / "audio.m4a"
                audio.write_bytes(b"synthetic-audio")
                values = {
                    "manifest.json": {
                        "status": "topics_allocated",
                        "source_url": inputs["audio_url"],
                        "source_sha256": module.digest(audio),
                        "model_generation_requests": 0,
                    },
                    "topics.json": {
                        "status": "topics_allocated",
                        "source_url": inputs["audio_url"],
                        "model_generation_requests": 0,
                        "topics": [],
                    },
                    "asr-receipt.json": {"key": key},
                    "transcripts/" + key + ".json": {"language": "ja", "segments": []},
                }
                for name, value in values.items():
                    (destination / name).write_text(json.dumps(value))
                (destination / "request-receipt.json").write_text(
                    json.dumps(
                        {
                            "request_id": inputs["request_id"],
                            "source_ref": inputs["source_ref"],
                            "status": "topics_allocated",
                            "audio_source_url": inputs["audio_url"],
                            "sha256": {
                                name: module.digest(destination / name)
                                for name in ["audio.m4a", *values]
                            },
                        }
                    )
                )
                if control["tamper"]:
                    audio.write_bytes(b"corrupted")
                return ""
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
                "--audio-analysis-url",
                KICK_URL,
                *extra,
            ],
        )
        module.main()

    return module, source, control, invoke


def assert_submission_lock(path, locked):
    with path.open("a") as probe:
        if locked:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("condition", ["private", "active"])
def test_rejects_public_repository_and_active_jobs(submitter, condition):
    _, _, control, invoke = submitter
    control[condition] = condition != "private"
    with pytest.raises(RuntimeError):
        invoke()
    assert not control["posts"] and not control["releases"]


@pytest.mark.parametrize("active, allowed", [(0, True), (1, False), (2, False)])
def test_requests_are_serialized_because_one_request_owns_several_runtimes(
    submitter, active, allowed
):
    _, _, control, invoke = submitter
    control["active"] = active
    if allowed:
        invoke()
        assert len(control["posts"]) == 1
    else:
        with pytest.raises(RuntimeError, match="another job"):
            invoke()
        assert not control["posts"] and not control["releases"]


def test_configuration_cannot_name_local_files(submitter, tmp_path):
    _, _, control, invoke = submitter
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"transcript_cache": "/home/someone/transcripts"}))
    with pytest.raises(RuntimeError, match="worker sets it"):
        invoke("--config", str(config))
    assert not control["releases"] and not control["posts"]


def test_configuration_cannot_upload_credentials(submitter, tmp_path):
    _, _, control, invoke = submitter
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"GEMINI_API_KEY": "synthetic-not-a-real-key"}))
    with pytest.raises(RuntimeError, match="unknown fields"):
        invoke("--config", str(config))
    assert not control["releases"] and not control["posts"]


def test_url_only_audio_dispatch_sends_settings_without_release(
    submitter, monkeypatch, tmp_path
):
    module, _, control, _ = submitter
    url = "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001"
    config = tmp_path / "config.json"
    config.write_text('{"asr_parts": 2, "clips": 4}')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            url,
            "--config",
            str(config),
        ],
    )
    monkeypatch.setattr(
        module, "digest", lambda *a: pytest.fail("URL-only submission read local media")
    )
    module.main()
    inputs = control["posts"][0]["inputs"]
    assert inputs == {
        "request_id": inputs["request_id"],
        "input_tag": "clipforge-input-" + inputs["request_id"],
        "source_ref": "a" * 40,
        "audio_url": url,
        "settings_json": inputs["settings_json"],
    }
    assert not control["releases"]
    assert not any(call[0] == "release" for call in control["calls"])
    state = tmp_path / ".local/state/clipforge" / inputs["request_id"]
    receipt = json.loads((state / "receipt.json").read_text())
    assert receipt["audio_url"] == url and receipt["input_staged"] is False
    request = json.loads((state / "request.json").read_text())
    assert request["audio_source_url"] == url
    assert request["video_sha256"] is None and request["video_bytes"] is None
    assert json.loads(inputs["settings_json"]) == request["settings"]
    assert request["settings"] == {
        **json.loads(Path("config.landscape.example.json").read_text()),
        "asr_parts": 2,
        "clips": 4,
    }
    assert control["playlists"] == ["lowest"] and control["preflight_closed"]
    assert control["preflight_budgets"] == [
        {"max_bytes": 6 * 1024**3, "max_seconds": 86400}
    ]
    assert len(inputs["settings_json"].encode("utf-8")) < module.MAX_SETTINGS_JSON_BYTES


@pytest.mark.parametrize(
    "duration, error, message",
    [
        (21601, None, "21601 seconds.*21600 seconds"),
        (
            5536,
            "archive playlist is still growing; retry after completion",
            "still growing",
        ),
        (5536, "Kick media HTTP 503; failed after 4 attempts", "HTTP 503.*4 attempts"),
    ],
)
def test_url_preflight_rejects_before_dispatch(
    submitter, tmp_path, monkeypatch, duration, error, message
):
    from clipforge.storage import PipelineError

    module, _, control, _ = submitter
    control["preflight_duration"] = duration
    control["preflight_error"] = PipelineError(error) if error else None
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
        ],
    )
    with pytest.raises(RuntimeError, match=message):
        module.main()
    assert not control["posts"] and not control["releases"]
    assert control["preflight_closed"] and control["playlists"] == ["lowest"]
    assert not list((tmp_path / ".local/state/clipforge").glob("*/request.json"))


@pytest.mark.parametrize("duration", [21600, 43200])
def test_url_preflight_accepts_configured_duration_boundary(
    submitter, tmp_path, monkeypatch, duration
):
    module, _, control, _ = submitter
    control["preflight_duration"] = duration
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"max_source_seconds": duration}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--config",
            str(config),
        ],
    )
    module.main()
    assert len(control["posts"]) == 1 and control["preflight_closed"]


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"max_source_seconds": 18000, "max_transport_bytes": 8 * 1024**3, "clips": 4},
    ],
)
def test_url_settings_match_submission_acquisition_and_analysis(
    submitter, tmp_path, monkeypatch, overrides
):
    module, _, control, _ = submitter
    config = tmp_path / "config.json"
    config.write_text(json.dumps(overrides))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--config",
            str(config),
        ],
    )
    module.main()
    inputs = control["posts"][0]["inputs"]
    state = tmp_path / ".local/state/clipforge" / inputs["request_id"]
    submitted = json.loads((state / "request.json").read_text())["settings"]
    expected = Settings.model_validate(submitted)
    assert submitted == {
        **json.loads(Path("config.landscape.example.json").read_text()),
        "asr_parts": 4,
        **overrides,
    }
    assert control["preflight_budgets"][0]["max_bytes"] == expected.max_transport_bytes
    monkeypatch.syspath_prepend(str(Path("tools").resolve()))
    acquisition = load_tool("acquire_audio_url")

    class VOD:
        def __init__(self, url, cache, **kwargs):
            assert kwargs == {
                "max_bytes": expected.max_transport_bytes,
                "max_seconds": expected.max_source_seconds,
            }

        def close(self):
            pass

    monkeypatch.setattr(acquisition, "KickVOD", VOD)
    monkeypatch.setattr(
        acquisition,
        "acquire",
        lambda vod, root: (root / "audio.m4a").write_bytes(b"synthetic-audio"),
    )
    directory = tmp_path / "acquired"
    acquisition.main(
        [
            "--url",
            inputs["audio_url"],
            "--request-id",
            inputs["request_id"],
            "--source-ref",
            inputs["source_ref"],
            "--settings-json",
            inputs["settings_json"],
            "--directory",
            str(directory),
        ]
    )
    acquired = json.loads((directory / "request.json").read_text())
    assert acquired["settings"] == submitted
    # The ASR coordinator reads the same settings from the acquired request.
    assert Settings.model_validate(acquired["settings"]) == expected


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"unexpected": True}, "unknown fields"),
        ({"transcript_cache": "synthetic"}, "worker sets it"),
        ({"heading": "あ" * 6000}, "16 KB"),
        ({"max_source_seconds": 43201}, "max_source_seconds.*43200"),
    ],
)
def test_url_only_audio_rejects_invalid_settings_before_dispatch(
    submitter, monkeypatch, tmp_path, settings, message
):
    module, _, control, _ = submitter
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--config",
            str(config),
        ],
    )
    with pytest.raises(RuntimeError, match=message):
        module.main()
    assert not control["posts"] and not control["releases"]


def test_settings_json_limit_counts_utf8_bytes(submitter):
    module = submitter[0]
    settings = {"heading": "あ"}
    value = json.dumps(settings, ensure_ascii=False)
    boundary = value + " " * (16384 - len(value.encode("utf-8")))
    assert module.parse_settings_json(boundary) == settings
    with pytest.raises(RuntimeError, match="16 KB"):
        module.parse_settings_json(boundary + " ")


@pytest.mark.parametrize(
    "url",
    [
        "http://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
        "https://example.invalid/example/videos/00000000-0000-0000-0000-000000000001",
        "https://kick.com/example/videos/not-a-uuid",
        "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001?extra=1",
    ],
)
def test_audio_dispatch_rejects_noncanonical_url(submitter, monkeypatch, url):
    from clipforge.storage import PipelineError

    module, _, control, _ = submitter
    argv = ["submit_job", "--audio-analysis-url", url]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(PipelineError, match="canonical"):
        module.main()
    assert not control["posts"] and not control["releases"]


@pytest.fixture
def chained_submitter(submitter, monkeypatch):
    module, _, control, _ = submitter
    monkeypatch.syspath_prepend(str(Path("tools").resolve()))
    original = module.gh
    control.update(
        analysis_conclusion="success", previews_conclusion="success", chain_fault=None
    )

    def gh(*args, **kwargs):
        if args[:2] == ("run", "view"):
            control["calls"].append(args)
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success"
                    if control["analysis_conclusion"]
                    == control["previews_conclusion"]
                    == "success"
                    else "failure",
                    "jobs": [
                        {"name": name, "conclusion": conclusion, "status": "completed"}
                        for name, conclusion in (
                            ("colab-auto", control["analysis_conclusion"]),
                            ("previews", control["previews_conclusion"]),
                        )
                    ],
                }
            )
        result = original(*args, **kwargs)
        if args[:2] != ("run", "download"):
            return result
        root = Path(args[args.index("--dir") + 1])
        receipt_path = root / "request-receipt.json"
        returned = json.loads(receipt_path.read_text())
        returned["workflow_run"] = "99"
        if args[args.index("--name") + 1].startswith("clipforge-result-"):
            manifest = json.loads((root / "manifest.json").read_text())
            manifest.update(
                status="awaiting_video_review", audio_stage="previews", asr_reused=True
            )
            (root / "manifest.json").write_text(json.dumps(manifest))
            cache_path = root / "asr-receipt.json"
            cache = json.loads(cache_path.read_text())
            cache["reused"] = True
            cache_path.write_text(json.dumps(cache))
            values = {
                "previews.json": {"status": "awaiting_video_review", "previews": []},
                "review-draft.json": {"clips": []},
                "review-draft-diagnostics.json": {"shortfall": 3},
            }
            for name, value in values.items():
                (root / name).write_text(json.dumps(value))
            returned.update(
                status="awaiting_video_review",
                audio_stage="previews",
                asr_reused=True,
                previous_run_id="99",
                previous_request_id=returned["request_id"],
            )
            returned["sha256"] = {
                name: module.digest(root / name)
                for name in [*returned["sha256"], *values]
            }
            fault = control["chain_fault"]
            if fault in {"workflow_run", "previous_run_id"}:
                returned[fault] = "100"
            elif fault in {"request_id", "previous_request_id"}:
                returned[fault] = "f" * 32
            elif fault == "source_ref":
                returned[fault] = "f" * 40
            elif fault == "url":
                returned["audio_source_url"] = returned["audio_source_url"].replace(
                    "example", "other"
                )
            elif fault == "hash":
                (root / "audio.m4a").write_bytes(b"modified synthetic audio")
            elif fault == "draft":
                (root / "review-draft.json").unlink()
                returned["sha256"].pop("review-draft.json")
        elif control["chain_fault"] == "analysis_hash":
            (root / "audio.m4a").write_bytes(b"modified synthetic audio")
        elif control["chain_fault"] == "analysis_run":
            returned["workflow_run"] = "100"
        receipt_path.write_text(json.dumps(returned))
        return result

    monkeypatch.setattr(module, "gh", gh)

    def invoke(*extra):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "submit_job",
                "--audio-analysis-url",
                "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
                "--auto-previews",
                *extra,
            ],
        )
        module.main()

    return module, control, invoke


@pytest.mark.parametrize(
    "fault",
    [
        "workflow_run",
        "previous_run_id",
        "request_id",
        "previous_request_id",
        "source_ref",
        "url",
        "hash",
        "draft",
    ],
)
def test_chained_collection_refuses_invalid_result(chained_submitter, tmp_path, fault):
    _, control, invoke = chained_submitter
    control["chain_fault"] = fault
    output = tmp_path / "previews"
    with pytest.raises(RuntimeError):
        invoke("--output", str(output))
    assert not output.exists()
    identity = control["posts"][0]["inputs"]["request_id"]
    receipt = json.loads(
        (tmp_path / ".local/state/clipforge" / identity / "receipt.json").read_text()
    )
    assert receipt["status"] == "DOWNLOAD_FAILED"


@pytest.mark.parametrize("fault", ["analysis_hash", "analysis_run", "analysis_failure"])
def test_partial_analysis_collection_refuses_invalid_intermediate(
    chained_submitter, tmp_path, monkeypatch, fault
):
    module, control, invoke = chained_submitter
    control["previews_conclusion"] = "failure"
    if fault == "analysis_failure":
        control["analysis_conclusion"] = "failure"
    else:
        control["chain_fault"] = fault
    invoke()
    identity = control["posts"][0]["inputs"]["request_id"]
    output = tmp_path / "analysis"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--collect",
            identity,
            "--collect-stage",
            "analysis",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(RuntimeError):
        module.main()
    assert not output.exists()
    receipt = json.loads(
        (tmp_path / ".local/state/clipforge" / identity / "receipt.json").read_text()
    )
    if fault == "analysis_failure":
        assert receipt["status"] == "FAILED"
        assert not any(c[:2] == ("run", "download") for c in control["calls"])
    else:
        assert receipt["status"] == "PARTIAL_FAILED"
        assert receipt["analysis_collection_status"] == "DOWNLOAD_FAILED"


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--collect", "c" * 32],
    ],
)
def test_auto_previews_requires_a_url_submission(submitter, monkeypatch, extra):
    module, _, control, _ = submitter
    monkeypatch.setattr(sys, "argv", ["submit_job", "--auto-previews", *extra])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2 and not control["posts"]


def test_chain_wait_timeout_is_configurable(chained_submitter, monkeypatch, tmp_path):
    module, control, invoke = chained_submitter
    clock = iter([0, 0, 2])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="waiting timed out"):
        invoke("--wait", "--wait-timeout", "1")
    identity = control["posts"][0]["inputs"]["request_id"]
    receipt = json.loads(
        (tmp_path / ".local/state/clipforge" / identity / "receipt.json").read_text()
    )
    assert receipt["status"] == "UNCONFIRMED" and receipt["run_id"] == 99


def test_audio_analysis_rejects_removed_settings(submitter, tmp_path):
    _, _, control, invoke = submitter
    config = tmp_path / "config.json"
    config.write_text('{"image_search": true}')
    with pytest.raises(RuntimeError, match="unknown fields"):
        invoke("--config", str(config))
    assert not control["releases"]


def test_github_failure_exposes_only_http_status(monkeypatch):
    module = load_tool("submit_job")
    monkeypatch.setattr(
        module.github_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1,
            stderr="HTTP 401: Bad credentials https://private.test/?signed=SECRET",
            stdout="",
        ),
    )
    with pytest.raises(RuntimeError, match=r"HTTP 401") as failure:
        module.gh("release", "create", "synthetic-tag")
    assert "SECRET" not in str(failure.value) and "https" not in str(failure.value)


def test_status_does_not_submit_or_download(submitter, monkeypatch, capsys):
    module, _, control, invoke = submitter
    invoke()
    identity = control["posts"][0]["inputs"]["request_id"]
    monkeypatch.setattr(sys, "argv", ["submit_job", "--status", identity])
    module.main()
    assert len(control["posts"]) == 1
    assert "github_status" in capsys.readouterr().out


def test_cleanup_rejects_unrelated_tags(submitter, monkeypatch):
    module, _, control, _ = submitter
    monkeypatch.setattr(
        sys, "argv", ["submit_job", "--cleanup-tag", "unrelated-release"]
    )
    with pytest.raises(RuntimeError):
        module.main()
    assert not control["releases"]


@pytest.mark.parametrize("status, submitted", [("403", True), ("500", False)])
def test_unreadable_secret_names_defer_to_the_worker_only_on_403(
    submitter, monkeypatch, status, submitted
):
    module, _, control, invoke = submitter
    original = module.gh

    def gh(*args, **kwargs):
        if args[:2] == ("secret", "list"):
            raise RuntimeError(f"GitHub operation failed: secret (HTTP {status})")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "gh", gh)
    if submitted:
        invoke()
        assert len(control["posts"]) == 1
    else:
        with pytest.raises(RuntimeError, match="HTTP 500"):
            invoke()
        assert not control["posts"]
