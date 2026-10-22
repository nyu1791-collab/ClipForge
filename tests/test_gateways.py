import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

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


def assert_submission_lock(path, locked):
    with path.open("a") as probe:
        if locked:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("wait", [False, True])
def test_submission_releases_lock_before_polling_and_download(
    submitter, tmp_path, monkeypatch, wait
):
    module, _, control, invoke = submitter
    original = module.gh
    lock = tmp_path / ".local/state/clipforge/.submit.lock"
    phases = []

    def gh(*args, **kwargs):
        if args[:2] == ("run", "list"):
            submitted = bool(control["posts"])
            assert_submission_lock(lock, locked=not submitted)
            phases.append("poll" if submitted else "check")
            if submitted:
                identity = control["posts"][0]["inputs"]["request_id"]
                receipt = json.loads(
                    (lock.parent / identity / "receipt.json").read_text()
                )
                assert receipt["status"] == "DISPATCHED"
                assert receipt["job_submitted"] is True
        elif args[:2] == ("release", "create") or args[:3] == (
            "api",
            "--method",
            "POST",
        ):
            assert_submission_lock(lock, locked=True)
            phases.append("submit")
        elif args[:2] in {("run", "view"), ("run", "download")}:
            assert_submission_lock(lock, locked=False)
            phases.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "gh", gh)
    invoke(*(["--wait", "--output", str(tmp_path / "result")] if wait else []))
    assert phases == ["check", "submit", "submit", "poll"] + (
        ["view", "download"] if wait else []
    )
    assert_submission_lock(lock, locked=False)


@pytest.mark.parametrize("condition", ["private", "active"])
def test_rejects_public_repository_and_active_jobs(submitter, condition):
    _, _, control, invoke = submitter
    control[condition] = condition != "private"
    with pytest.raises(RuntimeError):
        invoke("--worker", "cpu")
    assert not control["posts"] and not control["releases"]


@pytest.mark.parametrize("active, allowed", [(1, True), (2, False)])
def test_colab_jobs_run_two_at_once(submitter, active, allowed):
    _, _, control, invoke = submitter
    control["active"] = active
    if allowed:
        invoke("--worker", "colab-auto")
        assert len(control["posts"]) == 1
    else:
        with pytest.raises(RuntimeError, match="another video job"):
            invoke("--worker", "colab-auto")
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


@pytest.mark.parametrize("fault", ["symlink", "directory_symlink", "fifo"])
def test_video_download_rejects_symlinks_and_non_regular_entries(
    submitter, tmp_path, monkeypatch, fault
):
    module, _, control, invoke = submitter
    original = module.gh

    def gh(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("run", "download"):
            root = Path(args[args.index("--dir") + 1])
            if fault == "symlink":
                (root / "unlisted.txt").symlink_to("manifest.json")
            elif fault == "directory_symlink":
                (root / "alias").symlink_to("clips", target_is_directory=True)
            elif fault == "fifo":
                module.os.mkfifo(root / "pipe")
        return result

    monkeypatch.setattr(module, "gh", gh)
    destination = tmp_path / "result"
    with pytest.raises(RuntimeError, match="unexpected|symlink|non-regular"):
        invoke("--wait", "--output", str(destination))
    assert not destination.exists()
    receipt = (
        tmp_path
        / ".local/state/clipforge"
        / control["request"]["request_id"]
        / "receipt.json"
    )
    assert json.loads(receipt.read_text())["status"] == "DOWNLOAD_FAILED"


def test_video_download_allows_extra_regular_files_and_directories(
    submitter, tmp_path, monkeypatch
):
    module, _, _, invoke = submitter
    original = module.gh

    def gh(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("run", "download"):
            root = Path(args[args.index("--dir") + 1])
            (root / "transcripts").mkdir()
            (root / "transcripts/synthetic.json").write_text("{}")
            (root / "empty-directory").mkdir()
            for name in ("render.json", "quality.json", "ledger.json", "thumbnail.png"):
                (root / "clips/clip-001" / name).write_bytes(b"synthetic extra")
        return result

    monkeypatch.setattr(module, "gh", gh)
    destination = tmp_path / "result"
    invoke("--wait", "--output", str(destination))
    assert (destination / "transcripts/synthetic.json").is_file()
    assert (destination / "clips/clip-001/quality.json").is_file()


def test_gateway_settings_field_list_tracks_model(submitter):
    module = submitter[0]
    assert (
        set(Settings.model_fields) == module.SETTINGS_FIELDS | module.LOCAL_FILE_FIELDS
    )
    assert not module.SETTINGS_FIELDS & module.LOCAL_FILE_FIELDS


def test_configuration_cannot_name_local_files(submitter, tmp_path):
    _, _, control, invoke = submitter
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"bgm": "/home/someone/music.mp3"}))
    with pytest.raises(RuntimeError, match="local files"):
        invoke("--config", str(config))
    assert not control["releases"] and not control["posts"]


def test_configuration_cannot_upload_credentials(submitter, tmp_path):
    _, _, control, invoke = submitter
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"GEMINI_API_KEY": "synthetic-not-a-real-key"}))
    with pytest.raises(RuntimeError, match="unknown fields"):
        invoke("--config", str(config))
    assert not control["releases"] and not control["posts"]


@pytest.mark.parametrize("worker", ["cpu", "colab-auto"])
def test_audio_analysis_dispatch_has_no_model_or_secret_requirement(
    submitter, monkeypatch, tmp_path, worker
):
    module, _, control, invoke = submitter
    original = module.gh

    def gh(*args, **kwargs):
        if args[:2] == ("secret", "list") and worker == "cpu":
            pytest.fail("audio-only analysis checked Gemini credentials")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "gh", gh)
    url = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
    invoke("--audio-analysis-url", url, "--ref", "main", "--worker", worker)
    assert control["request"]["audio_source_url"] == url
    assert control["request"]["settings"]["gemini_model"] == "manual-no-api"
    assert len(control["posts"]) == 1
    assert len(control["releases"]) == 1
    assert control["posts"][0]["inputs"]["audio_url"] == ""
    state = tmp_path / ".local/state/clipforge" / control["request"]["request_id"]
    receipt = json.loads((state / "receipt.json").read_text())
    assert receipt["audio_url"] == "" and receipt["input_staged"] is True


def test_url_only_audio_dispatch_sends_settings_without_release(
    submitter, monkeypatch, tmp_path
):
    module, _, control, _ = submitter
    url = "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001"
    config = tmp_path / "config.json"
    config.write_text('{"asr_batch_size": 8, "threads": 4}')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            url,
            "--worker",
            "colab-auto",
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
        "worker": "colab-auto",
        "audio_url": url,
        "settings_json": inputs["settings_json"],
        "public_cache": "off",
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
        "asr_batch_size": 8,
        "threads": 4,
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
        {"max_source_seconds": 18000, "max_transport_bytes": 8 * 1024**3, "threads": 4},
    ],
)
def test_url_settings_match_submission_acquisition_and_analysis(
    submitter, tmp_path, monkeypatch, overrides
):
    from clipforge import audio_first
    from clipforge.storage import write_json

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

    def allocate(source, root, settings, source_url):
        assert settings == expected
        assert source_url == inputs["audio_url"]
        write_json(root / "asr-receipt.json", {"key": "e" * 64})
        write_json(
            root / "transcripts" / ("e" * 64 + ".json"),
            {"language": "ja", "segments": []},
        )
        result = {"status": "topics_allocated", "topics": []}
        write_json(root / "topics.json", result)
        return result, None

    monkeypatch.setattr(audio_first, "allocate_audio", allocate)
    runner = load_tool("run_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", runner.PRIVATE_REPO)
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", inputs["source_ref"])
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.delenv("CLIPFORGE_REQUIRE_CUDA", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_request",
            "run",
            "--tag",
            inputs["input_tag"],
            "--directory",
            str(directory),
            "--output",
            str(tmp_path / "analysis"),
        ],
    )
    runner.main()


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"unexpected": True}, "unknown fields"),
        ({"bgm": "synthetic.mp3"}, "local files"),
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


def test_retired_worker_is_rejected_before_remote_access(submitter):
    _, _, control, invoke = submitter
    with pytest.raises(SystemExit) as error:
        invoke("--worker", "colab-gpu")
    assert error.value.code == 2
    assert not control["calls"]


def test_url_only_audio_requires_colab_auto(submitter, monkeypatch):
    module, _, control, _ = submitter
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--worker",
            "cpu",
        ],
    )
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert not control["posts"] and not control["releases"]


@pytest.mark.parametrize("local_input", [False, True])
@pytest.mark.parametrize(
    "url",
    [
        "http://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
        "https://example.invalid/example/videos/00000000-0000-0000-0000-000000000001",
        "https://kick.com/example/videos/not-a-uuid",
        "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001?extra=1",
    ],
)
def test_audio_dispatch_rejects_noncanonical_url(
    submitter, monkeypatch, local_input, url
):
    from clipforge.storage import PipelineError

    module, source, control, _ = submitter
    argv = ["submit_job", "--audio-analysis-url", url]
    if local_input:
        argv.extend(["--input", str(source)])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(PipelineError, match="canonical Kick VOD URL"):
        module.main()
    assert not control["posts"] and not control["releases"]


@pytest.mark.parametrize("explicit_output", [False, True])
def test_url_only_audio_wait_collects_verified_analysis(
    submitter, monkeypatch, tmp_path, explicit_output
):
    module, _, control, _ = submitter
    argv = [
        "submit_job",
        "--audio-analysis-url",
        "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
    ]
    argv.extend(
        ["--output", str(tmp_path / "result")] if explicit_output else ["--wait"]
    )
    monkeypatch.setattr(sys, "argv", argv)
    module.main()
    identity = control["posts"][0]["inputs"]["request_id"]
    state = tmp_path / ".local/state/clipforge" / identity
    receipt = json.loads((state / "receipt.json").read_text())
    destination = tmp_path / "result" if explicit_output else state / "result"
    assert receipt["status"] == "COMPLETE" and receipt["input_staged"] is False
    assert (destination / "audio.m4a").read_bytes() == b"synthetic-audio"
    assert (destination / "topics.json").is_file()
    assert not control["releases"]


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


def test_auto_previews_dispatch_receipt_and_verified_collection(
    chained_submitter, tmp_path
):
    _, control, invoke = chained_submitter
    output = tmp_path / "previews"
    invoke("--output", str(output))
    inputs = control["posts"][0]["inputs"]
    assert inputs["auto_previews"] is True
    state = tmp_path / ".local/state/clipforge" / inputs["request_id"]
    receipt = json.loads((state / "receipt.json").read_text())
    assert receipt["target_stage"] == "previews" and receipt["status"] == "COMPLETE"
    assert receipt["analysis_status"] == receipt["previews_status"] == "success"
    assert (output / "review-draft.json").is_file()
    assert len(control["posts"]) == 1 and not control["releases"]
    download = next(c for c in control["calls"] if c[:2] == ("run", "download"))
    assert (
        download[download.index("--name") + 1]
        == "clipforge-result-" + inputs["request_id"]
    )


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


@pytest.mark.parametrize("preview_status", ["failure", "cancelled", "timed_out"])
def test_partial_failure_can_collect_only_verified_analysis(
    chained_submitter, tmp_path, monkeypatch, preview_status
):
    module, control, invoke = chained_submitter
    control["previews_conclusion"] = preview_status
    with pytest.raises(RuntimeError, match="did not complete successfully"):
        invoke("--wait")
    identity = control["posts"][0]["inputs"]["request_id"]
    state = tmp_path / ".local/state/clipforge" / identity
    receipt = json.loads((state / "receipt.json").read_text())
    assert receipt["status"] == "PARTIAL_FAILED"
    assert (
        receipt["analysis_status"] == "success"
        and receipt["previews_status"] == preview_status
    )
    assert not any(c[:2] == ("run", "download") for c in control["calls"])
    output = tmp_path / "recovered"
    monkeypatch.setattr(
        sys, "argv", ["submit_job", "--collect", identity, "--output", str(output)]
    )
    with pytest.raises(RuntimeError, match="not completed successfully"):
        module.main()
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
    module.main()
    receipt = json.loads((state / "receipt.json").read_text())
    assert receipt["status"] == "PARTIAL_FAILED"
    assert receipt["analysis_collection_status"] == "COMPLETE"
    assert receipt["analysis_output"] == str(output)
    assert (output / "audio.m4a").read_bytes() == b"synthetic-audio"
    download = next(c for c in control["calls"] if c[:2] == ("run", "download"))
    assert download[download.index("--name") + 1] == "clipforge-analysis-" + identity
    assert len(control["posts"]) == 1


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
        [
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--input",
            "synthetic.m4a",
        ],
        [
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--worker",
            "cpu",
        ],
    ],
)
def test_auto_previews_requires_url_only_colab_submission(
    submitter, monkeypatch, extra
):
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


def test_chain_default_wait_allows_analysis_and_cpu_stage(
    chained_submitter, monkeypatch
):
    module, _, invoke = chained_submitter
    clock = iter([0, 0, 17000])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    invoke("--wait")


def test_url_only_audio_corruption_can_be_collected_again(
    submitter, monkeypatch, tmp_path
):
    module, _, control, _ = submitter
    destination = tmp_path / "result"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--output",
            str(destination),
        ],
    )
    control["tamper"] = True
    with pytest.raises(RuntimeError, match="checksum"):
        module.main()
    assert not destination.exists()
    identity = control["posts"][0]["inputs"]["request_id"]
    control["tamper"] = False
    monkeypatch.setattr(
        sys, "argv", ["submit_job", "--collect", identity, "--output", str(destination)]
    )
    module.main()
    assert (destination / "audio.m4a").is_file()
    assert len(control["posts"]) == 1 and not control["releases"]


@pytest.mark.parametrize(
    "fault", ["source_url", "source_sha256", "receipt_url", "receipt_audio_hash"]
)
def test_url_only_collection_checks_runner_audio_hash_and_submitted_url(
    submitter, monkeypatch, tmp_path, fault
):
    module, _, _, _ = submitter
    original = module.gh

    def gh(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("run", "download"):
            root = Path(args[args.index("--dir") + 1])
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            other_url = (
                "https://kick.com/other/videos/00000000-0000-0000-0000-000000000002"
            )
            if fault in {"source_url", "source_sha256"}:
                manifest[fault] = other_url if fault == "source_url" else "f" * 64
            manifest_path.write_text(json.dumps(manifest))
            receipt_path = root / "request-receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["sha256"]["manifest.json"] = module.digest(manifest_path)
            if fault == "receipt_url":
                receipt["audio_source_url"] = other_url
            elif fault == "receipt_audio_hash":
                receipt["sha256"]["audio.m4a"] = "f" * 64
            receipt_path.write_text(json.dumps(receipt))
        return result

    monkeypatch.setattr(module, "gh", gh)
    destination = tmp_path / "result"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_job",
            "--audio-analysis-url",
            "https://kick.com/example/videos/00000000-0000-0000-0000-000000000001",
            "--output",
            str(destination),
        ],
    )
    with pytest.raises(RuntimeError, match="identity|checksum"):
        module.main()
    assert not destination.exists()


def test_audio_analysis_rejects_network_enrichment(submitter, tmp_path):
    _, _, control, invoke = submitter
    config = tmp_path / "config.json"
    config.write_text('{"image_search": true}')
    with pytest.raises(RuntimeError, match="external lookups"):
        invoke(
            "--audio-analysis-url",
            "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5",
            "--config",
            str(config),
        )
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


def test_staging_failure_records_no_job_and_never_dispatches(
    submitter, monkeypatch, tmp_path
):
    module, _, control, invoke = submitter
    original = module.gh

    def gh(*args, **kwargs):
        if args[:2] == ("release", "create"):
            raise RuntimeError("GitHub operation failed: release (HTTP 401)")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "gh", gh)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        invoke()
    receipts = list((tmp_path / ".local/state/clipforge").glob("*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "STAGING_FAILED" and "run_id" not in receipt
    assert not control["posts"]


@pytest.fixture
def audio_recovery(submitter, tmp_path, monkeypatch):
    module, source, _, _ = submitter
    identity = "c" * 32
    state = tmp_path / ".local/state/clipforge" / identity
    state.mkdir(parents=True)
    request = {
        "request_id": identity,
        "source_ref": "a" * 40,
        "video_sha256": module.digest(source),
        "video_bytes": source.stat().st_size,
        "settings": {"gemini_model": "manual-no-api"},
        "chat_sha256": None,
        "audio_source_url": "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5",
    }
    module.save(state / "request.json", request)
    module.save(
        state / "receipt.json",
        {
            "repository": module.REPO,
            "request_id": identity,
            "source_ref": "a" * 40,
            "input_tag": "clipforge-input-" + identity,
            "status": "UPLOAD_FAILED_AUTH",
        },
    )
    control = {
        "assets": [],
        "uploads": [],
        "posts": [],
        "existing": False,
        "active": False,
        "upload_error": False,
        "upload_fault": None,
        "dispatch_timeout": False,
        "missing_release": False,
    }

    def asset(name):
        path = source if name == "input-video.bin" else state / "request.json"
        return {
            "name": name,
            "size": path.stat().st_size,
            "digest": "sha256:" + module.digest(path),
        }

    def gh(*args, body=None, timeout=None):
        if args[:2] == ("api", "user"):
            return "nyu1791-collab"
        if args[:1] == ("api",) and args[1].count("/") == 2 and "--jq" in args:
            return "true"
        if args[:2] == ("repo", "view"):
            return '{"isPrivate":true}'
        if args[:2] == ("run", "list"):
            if args[-1] == "status":
                return json.dumps(
                    [{"status": "in_progress"}] if control["active"] else []
                )
            return json.dumps(
                [
                    {
                        "databaseId": 99,
                        "displayTitle": "ClipForge " + identity,
                        "event": "workflow_dispatch",
                    }
                ]
                if control["existing"]
                else []
            )
        if args[0] == "api" and "/releases/tags/" in args[1]:
            if control["missing_release"]:
                raise RuntimeError("GitHub operation failed: api (HTTP 404)")
            return json.dumps({"id": 123, "assets": control["assets"]})
        if args[:3] == ("api", "--method", "POST"):
            if args[3].endswith("/releases"):
                control["missing_release"] = False
                return json.dumps({"id": 123, "assets": []})
            control["posts"].append(json.loads(body))
            if control["dispatch_timeout"]:
                raise TimeoutError("response lost")
            return ""
        if args[:2] == ("release", "upload"):
            if control["upload_fault"] is not None:
                raise control["upload_fault"]
            if control["upload_error"]:
                raise RuntimeError("GitHub operation failed: release (HTTP 401)")
            for path in args[3 : args.index("--repo")]:
                name = Path(path).name
                control["uploads"].append(name)
                control["assets"].append(asset(name))
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(module, "gh", gh)

    def invoke():
        monkeypatch.setattr(
            sys,
            "argv",
            ["submit_job", "--resume-audio", identity, "--input", str(source)],
        )
        module.main()

    return module, source, state, control, asset, invoke


def test_audio_recovery_uploads_only_missing_asset_and_preserves_request(
    audio_recovery,
):
    module, _, state, control, asset, invoke = audio_recovery
    control["assets"] = [asset("input-video.bin")]
    before = (state / "request.json").read_bytes()
    invoke()
    assert control["uploads"] == ["request.json"]
    assert len(control["posts"]) == 1
    assert control["posts"][0]["inputs"]["request_id"] == "c" * 32
    assert (state / "request.json").read_bytes() == before
    assert json.loads((state / "receipt.json").read_text())["status"] == "DISPATCHED"
    control["existing"] = True
    invoke()
    assert len(control["posts"]) == 1 and control["uploads"] == ["request.json"]


def test_audio_recovery_releases_lock_after_dispatch_and_refuses_duplicate(
    audio_recovery, monkeypatch
):
    module, _, state, control, _, invoke = audio_recovery
    original = module.gh
    lock = state.parent / ".submit.lock"

    def gh(*args, **kwargs):
        if args[:2] in {("run", "list"), ("release", "upload")} or args[:3] == (
            "api",
            "--method",
            "POST",
        ):
            assert_submission_lock(lock, locked=True)
        return original(*args, **kwargs)

    def print_resumed(message):
        assert message.startswith("Resumed private audio request")
        assert_submission_lock(lock, locked=False)
        assert (
            json.loads((state / "receipt.json").read_text())["status"] == "DISPATCHED"
        )

    monkeypatch.setattr(module, "gh", gh)
    monkeypatch.setattr(module, "print", print_resumed, raising=False)
    invoke()
    with pytest.raises(RuntimeError, match="uncertain or not resumable"):
        invoke()
    assert len(control["posts"]) == 1


@pytest.mark.parametrize(
    "fault",
    ["input", "asset", "active", "unknown_state", "existing", "upload", "identity"],
)
def test_audio_recovery_refuses_duplicate_or_mismatched_work(audio_recovery, fault):
    module, source, state, control, asset, invoke = audio_recovery
    if fault == "input":
        source.write_bytes(b"changed")
    elif fault == "asset":
        control["assets"] = [{**asset("input-video.bin"), "digest": "sha256:wrong"}]
    elif fault == "active":
        control["active"] = True
    elif fault in {"unknown_state", "identity"}:
        receipt = json.loads((state / "receipt.json").read_text())
        receipt["status" if fault == "unknown_state" else "repository"] = "unknown"
        module.save(state / "receipt.json", receipt)
    elif fault == "existing":
        control["existing"] = True
        invoke()
        assert not control["uploads"] and not control["posts"]
        return
    else:
        control["upload_error"] = True
    with pytest.raises(RuntimeError):
        invoke()
    assert not control["posts"]


@pytest.mark.parametrize(
    "error, status",
    [
        (
            RuntimeError("GitHub operation failed: release (HTTP 401)"),
            "UPLOAD_FAILED_AUTH",
        ),
        (
            RuntimeError("GitHub operation failed: release (HTTP 502)"),
            "STAGING_PARTIAL",
        ),
        (TimeoutError("upload timed out"), "STAGING_PARTIAL"),
    ],
)
def test_audio_upload_failure_status_names_only_the_observed_cause(
    audio_recovery, error, status
):
    module, _, state, control, asset, invoke = audio_recovery
    control["assets"] = [asset("input-video.bin")]
    control["upload_fault"] = error
    with pytest.raises(type(error)):
        invoke()
    assert json.loads((state / "receipt.json").read_text())["status"] == status
    assert not control["posts"]
    # Every non-dispatched failure stays resumable with the same request ID.
    control["upload_fault"] = None
    invoke()
    assert control["uploads"] == ["request.json"] and len(control["posts"]) == 1
    assert json.loads((state / "receipt.json").read_text())["status"] == "DISPATCHED"


def test_audio_recovery_lost_dispatch_response_is_not_resubmitted(audio_recovery):
    _, _, state, control, _, invoke = audio_recovery
    control["dispatch_timeout"] = True
    with pytest.raises(TimeoutError):
        invoke()
    assert (
        json.loads((state / "receipt.json").read_text())["status"]
        == "DISPATCH_UNCONFIRMED"
    )
    with pytest.raises(RuntimeError, match="uncertain"):
        invoke()
    assert len(control["posts"]) == 1


def test_audio_recovery_can_recreate_missing_empty_release(audio_recovery):
    _, _, _, control, _, invoke = audio_recovery
    control["missing_release"] = True
    invoke()
    assert sorted(control["uploads"]) == ["input-video.bin", "request.json"]
    assert len(control["posts"]) == 1


def test_staging_status_does_not_require_a_workflow(
    audio_recovery, monkeypatch, capsys
):
    module, _, _, control, _, _ = audio_recovery
    monkeypatch.setattr(sys, "argv", ["submit_job", "--status", "c" * 32])
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert (
        result["status"] == "UPLOAD_FAILED_AUTH" and result["workflow_found"] is False
    )
    assert not control["uploads"] and not control["posts"]


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
        if args[:1] == ("api",) and args[1].count("/") == 2 and "--jq" in args:
            return "true" if control["private"] else "false"
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


def test_private_runner_audio_analysis_cannot_enter_video_or_model_path(
    runner, tmp_path, monkeypatch
):
    from clipforge import audio_first
    from clipforge.storage import write_json

    _, control, invoke = runner
    url = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
    control["request"]["audio_source_url"] = url
    invoke("prepare")
    monkeypatch.setattr(
        pipeline,
        "run",
        lambda *a, **k: pytest.fail("audio analysis entered video/model pipeline"),
    )

    def allocate(source, root, settings, source_url):
        assert source_url == url
        write_json(root / "asr-receipt.json", {"key": "e" * 64})
        write_json(
            root / "transcripts" / ("e" * 64 + ".json"),
            {"language": "ja", "segments": []},
        )
        result = {
            "status": "topics_allocated",
            "source_url": url,
            "topics": [],
            "model_generation_requests": 0,
        }
        write_json(root / "topics.json", result)
        return result, None

    monkeypatch.setattr(audio_first, "allocate_audio", allocate)
    invoke("run")
    output = tmp_path / "output"
    result = json.loads((output / "request-receipt.json").read_text())
    assert result["status"] == "topics_allocated" and result["rendered_clips"] == 0
    submit = load_tool("submit_job")
    submit.verify_download(output, control["request"], control["request"])
    (output / "transcripts" / ("e" * 64 + ".json")).write_text(
        '{"language":"ja", "segments":[]}'
    )
    with pytest.raises(RuntimeError, match="checksum"):
        submit.verify_download(output, control["request"], control["request"])


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


def test_workflow_input_cleanup_checks_tag_and_privacy_and_reports_failure():
    import yaml

    root = Path(__file__).resolve().parents[1] / "deploy"
    checked = 0
    for name in ("clipforge.yml", "audio-stages.yml"):
        workflow = yaml.safe_load((root / name).read_text())
        for job in workflow["jobs"].values():
            steps = job["steps"]
            cleanup = [s for s in steps if s.get("id") == "input-cleanup"]
            if not cleanup:
                continue
            script = cleanup[0]["run"]
            # Deletion needs only the preinstalled gh, a valid tag and a private repository.
            assert "^clipforge-input-[a-f0-9]{32}$" in script
            assert '--jq .private)" != true' in script
            assert "run_request.py" not in script and ".venv" not in script
            assert cleanup[0]["if"] == "always()"
            reports = [
                s
                for s in steps
                if s.get("if") == "always() && steps.input-cleanup.outcome != 'success'"
            ]
            assert len(reports) == 1 and "GITHUB_STEP_SUMMARY" in reports[0]["run"]
            checked += 1
    assert checked == 3


@pytest.mark.parametrize("upload_fails", [False, True])
def test_api_diagnostic_never_generates(tmp_path, monkeypatch, upload_fails):
    from types import SimpleNamespace
    from google import genai
    from google.genai import errors, types

    module = load_tool("check_gemini_connection")
    monkeypatch.setenv("GITHUB_REPOSITORY", "nyu1791-collab/ai-model-workers")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-only-not-a-secret")
    counts = {"uploads": 0, "count_tokens": 0, "deleted": 0}

    def upload(**kwargs):
        counts["uploads"] += 1
        if upload_fails:
            raise errors.ClientError(
                404, {"error": {"message": "synthetic upload not found"}}
            )
        return types.File(
            name="files/synthetic",
            uri="https://example.invalid/test",
            state=types.FileState.ACTIVE,
        )

    def count(**kwargs):
        counts["count_tokens"] += 1
        return SimpleNamespace(total_tokens=42)

    def delete(**kwargs):
        counts["deleted"] += 1

    models = SimpleNamespace(
        get=lambda **kw: SimpleNamespace(name="models/test"), count_tokens=count
    )
    client = SimpleNamespace(
        models=models,
        files=SimpleNamespace(upload=upload, delete=delete),
        close=lambda: None,
    )
    monkeypatch.setattr(genai, "Client", lambda **kw: client)
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(b"synthetic")
    report = module.diagnose(video, "test-video-model", tmp_path / "report.json")
    assert report["generation_requests"] == 0
    assert report["status"] == ("failed" if upload_fails else "complete")
    assert counts["count_tokens"] == (0 if upload_fails else 1)
    assert counts["deleted"] == (0 if upload_fails else 1)
    assert "synthetic-only-not-a-secret" not in (tmp_path / "report.json").read_text()


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
    url = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
    if submitted:
        invoke("--audio-analysis-url", url, "--worker", "colab-auto")
        assert control["posts"][0]["inputs"]["worker"] == "colab-auto"
    else:
        with pytest.raises(RuntimeError, match="HTTP 500"):
            invoke("--audio-analysis-url", url, "--worker", "colab-auto")
        assert not control["posts"]
