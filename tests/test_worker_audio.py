import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import audio_first, pipeline, signals, worker_audio as worker
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json

URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
IDENTITY = "b" * 32


@pytest.fixture
def previous_analysis(tmp_path, monkeypatch):
    previous = tmp_path / "previous"
    previous.mkdir()
    audio = previous / "audio.m4a"
    audio.write_bytes(b"synthetic acquired audio")
    info = {"audio": True, "duration": 300, "audio_start": 0}
    settings = Settings.model_validate_json(
        Path("config.landscape.example.json").read_text()
    )
    key = pipeline.transcript_identity(audio, info, settings)
    write_json(
        previous / "transcripts" / (key + ".json"),
        {
            "language": "ja",
            "segments": [
                {
                    "start": n,
                    "end": n + 20,
                    "text": "合成の話題",
                    "words": [{"start": n, "end": n + 20, "text": "合成の話題"}],
                }
                for n in (0, 100, 200)
            ],
        },
    )
    write_json(previous / "asr-receipt.json", {"key": key, "reused": False})
    write_json(previous / "topics.json", {"topics": []})
    manifest = {
        "status": "topics_allocated",
        "model_generation_requests": 0,
        "source_url": URL,
        "source_sha256": digest(audio),
    }
    write_json(previous / "manifest.json", manifest)
    files = [
        "audio.m4a",
        "asr-receipt.json",
        "topics.json",
        "manifest.json",
        "transcripts/" + key + ".json",
    ]
    receipt = {
        "request_id": IDENTITY,
        "status": "topics_allocated",
        "audio_source_url": URL,
        "sha256": {name: digest(previous / name) for name in files},
    }
    write_json(previous / "request-receipt.json", receipt)
    monkeypatch.setattr(audio_first, "audio_info", lambda path: info)
    monkeypatch.setattr(
        signals,
        "transcribe",
        lambda *a, **k: pytest.fail("a downstream stage started ASR"),
    )
    return previous, tmp_path / "output", settings


def test_restore_verifies_original_audio_and_private_analysis(previous_analysis):
    previous, output, _ = previous_analysis
    output.mkdir()
    assert worker.restore_analysis(previous, output, IDENTITY) == URL
    assert (output / "audio.m4a").read_bytes() == (previous / "audio.m4a").read_bytes()
    assert not (output / "manifest.json").exists()
    assert not (output / "request-receipt.json").exists()


@pytest.mark.parametrize(
    "fault", ["wrong_id", "file", "source", "missing_hash", "traversal"]
)
def test_restore_refuses_mismatched_analysis(previous_analysis, fault):
    previous, output, _ = previous_analysis
    output.mkdir()
    receipt_path = previous / "request-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    if fault == "wrong_id":
        receipt["request_id"] = "c" * 32
    elif fault == "file":
        (previous / "audio.m4a").write_bytes(b"different")
    elif fault == "source":
        receipt["audio_source_url"] = URL.replace("example", "other")
    elif fault == "missing_hash":
        receipt["sha256"].pop("audio.m4a")
    else:
        receipt["sha256"]["../escape"] = "a" * 64
    write_json(receipt_path, receipt)
    with pytest.raises(PipelineError):
        worker.restore_analysis(previous, output, IDENTITY)


def test_preview_stage_cannot_retranscribe_or_fetch_full_audio(
    previous_analysis, monkeypatch
):
    previous, output, settings = previous_analysis
    calls = []
    monkeypatch.setattr(
        worker, "KickVOD", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )

    def previews(vod, root, settings, analysis_result=None):
        calls.append(analysis_result)
        return {
            "status": "awaiting_video_review",
            "previews": [],
            "analysis_key": analysis_result[0]["analysis_key"],
        }

    monkeypatch.setattr(audio_first, "previews", previews)
    monkeypatch.setattr(worker, "preview_contact_sheet", lambda *a: None)
    result = worker.execute(previous, output, IDENTITY, settings, "previews")
    assert result["asr_reused"] is True and result["model_generation_requests"] == 0
    assert calls[0][1].segments
    assert json.loads((output / "asr-receipt.json").read_text())["reused"] is True


def test_later_stages_keep_how_the_analysis_recognised(previous_analysis, monkeypatch):
    previous, output, settings = previous_analysis
    path = previous / "asr-receipt.json"
    cache = json.loads(path.read_text())
    split = {"method": "vad", "silence": [7389.6, 7400.0]}
    cache.update(source="split_asr", parts=2, split=split, split_key="d" * 64)
    write_json(path, cache)
    receipt = json.loads((previous / "request-receipt.json").read_text())
    receipt["sha256"]["asr-receipt.json"] = digest(path)
    write_json(previous / "request-receipt.json", receipt)
    monkeypatch.setattr(
        worker, "KickVOD", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(
        audio_first,
        "previews",
        lambda vod, root, settings, analysis_result=None: {
            "status": "awaiting_video_review",
            "previews": [],
            "analysis_key": analysis_result[0]["analysis_key"],
        },
    )
    monkeypatch.setattr(worker, "preview_contact_sheet", lambda *a: None)
    worker.execute(previous, output, IDENTITY, settings, "previews")
    kept = json.loads((output / "asr-receipt.json").read_text())
    assert kept["reused"] is True and kept["source"] == "transcript_cache"
    assert kept["origin"] == {
        "source": "split_asr",
        "parts": 2,
        "split": split,
        "split_key": "d" * 64,
    }


@pytest.mark.parametrize("same_run", [False, True])
def test_previews_write_unapproved_drafts_before_receipting(
    previous_analysis, monkeypatch, same_run
):
    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    monkeypatch.setattr(worker, "gh", _previous_gh(previous))
    monkeypatch.setattr(
        worker, "KickVOD", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(worker, "preview_contact_sheet", lambda *a: None)

    def previews(vod, root, settings, analysis_result=None):
        analysis, _ = analysis_result
        rows = []
        for topic in analysis["topics"]:
            path = root / "previews" / (topic["id"] + ".mp4")
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"synthetic preview")
            rows.append(
                {
                    "topic_id": topic["id"],
                    "file": str(path.relative_to(root)),
                    "receipt": {"sha256": digest(path)},
                }
            )
        value = {
            "status": "awaiting_video_review",
            "analysis_key": analysis["analysis_key"],
            "previews": rows,
            "video_review_performed": False,
        }
        write_json(root / "previews.json", value)
        return value

    monkeypatch.setattr(audio_first, "previews", previews)
    args = _previews_args(output)
    if same_run:
        args = same_run_args(previous, output, monkeypatch)
    assert worker.main(args) == 0
    draft = json.loads((output / "review-draft.json").read_text())
    diagnostics = json.loads((output / "review-draft-diagnostics.json").read_text())
    receipt = json.loads((output / "request-receipt.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert draft["clips"]
    assert all(row["video_reviewed"] is False for row in draft["clips"])
    assert diagnostics["transcript_boundary_check"] == "performed"
    assert diagnostics["semantic_topic_review"] == "required"
    # The fixture audio is not decodable, so the draft proceeds without loudness.
    assert diagnostics["volume_hints"]["data"] == "not_available"
    assert diagnostics["volume_hints"]["reason"] == "decode_failed"
    assert manifest["status"] == "awaiting_video_review"
    assert manifest["human_review_performed"] is False
    assert (
        json.loads((output / "topics.json").read_text())["semantic_topic_review"]
        == "required"
    )
    for name in ("review-draft.json", "review-draft-diagnostics.json"):
        assert receipt["sha256"][name] == digest(output / name)
    if same_run:
        monkeypatch.syspath_prepend(str(Path("tools").resolve()))
        from submit_audio_stage import verify_stage_artifact

        assert not (output / ".state").exists()
        verify_stage_artifact(
            output,
            IDENTITY,
            "previews",
            {
                "source_ref": SOURCE_REF,
                "previous_run_id": "123",
                "previous_request_id": IDENTITY,
            },
            "123",
        )


def test_recorded_transcript_is_reused_after_asr_changes_without_recognition(
    previous_analysis, monkeypatch
):
    from clipforge import signals

    previous, output, settings = previous_analysis
    settings.threads = 4
    monkeypatch.setattr(
        signals, "transcribe", lambda *a, **k: pytest.fail("recognition started")
    )

    class Reached(Exception):
        pass

    def network(*args, **kwargs):
        raise Reached

    monkeypatch.setattr(worker, "KickVOD", network)
    with pytest.raises(Reached):
        worker.execute(previous, output, IDENTITY, settings, "previews")
    receipt = json.loads((output / "asr-receipt.json").read_text())
    assert receipt["reused"] is True


def test_missing_recorded_transcript_stops_before_network_or_recognition(
    previous_analysis, monkeypatch
):
    from clipforge import signals

    previous, output, settings = previous_analysis
    for path in (previous / "transcripts").glob("*.json"):
        path.unlink()
    monkeypatch.setattr(
        signals, "transcribe", lambda *a, **k: pytest.fail("recognition started")
    )
    monkeypatch.setattr(
        worker,
        "KickVOD",
        lambda *a, **k: pytest.fail("network started before cache validation"),
    )
    # The analysis check finds the listed transcript missing before anything starts.
    with pytest.raises((PipelineError, OSError)):
        worker.execute(previous, output, IDENTITY, settings, "previews")


@pytest.mark.parametrize(
    "workflow, branch",
    [
        ("ClipForge audio-first stages", "main"),
        ("ClipForge", "main"),
    ],
)
def test_worker_main_downloads_only_a_completed_private_analysis(
    previous_analysis, monkeypatch, capsys, workflow, branch
):
    import shutil

    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)

    def gh(*args):
        if args[:2] == ("run", "view"):
            assert args[-2:] == ("--json", worker.RUN_FIELDS)
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + IDENTITY,
                    "event": "workflow_dispatch",
                    "workflowName": workflow,
                    "headBranch": branch,
                }
            )
        destination = Path(args[args.index("--dir") + 1])
        shutil.copytree(previous, destination)
        return ""

    monkeypatch.setattr(worker, "gh", gh)

    def execute(previous, output, *args):
        output.mkdir()
        (output / "audio.m4a").write_bytes(b"synthetic")
        return {"status": "awaiting_video_review", "source_url": URL}

    monkeypatch.setattr(worker, "execute", execute)
    args = [
        "--stage",
        "previews",
        "--previous-run",
        "123",
        "--previous-request",
        IDENTITY,
        "--request-id",
        "c" * 32,
        "--source-ref",
        "a" * 40,
        "--output",
        str(output),
    ]
    assert worker.main(args) == 0
    receipt = json.loads((output / "request-receipt.json").read_text())
    assert receipt["asr_reused"] is True and receipt["audio_stage"] == "previews"
    assert receipt["previous_run_id"] == "123"
    assert receipt["previous_request_id"] == IDENTITY
    assert "request-receipt.json" not in receipt["sha256"]
    assert json.loads(capsys.readouterr().out)["model_generation_requests"] == 0
    monkeypatch.setenv("GITHUB_REPOSITORY", "public/wrong")
    assert worker.main(args) == 1


def test_contact_sheet_is_labelled_samples_not_full_video_attestation(
    tmp_path, monkeypatch
):
    from PIL import Image

    root = tmp_path
    (root / "previews").mkdir()
    (root / "previews/topic-001.mp4").write_bytes(b"synthetic preview")
    monkeypatch.setattr(worker.media, "probe", lambda *a: {"duration": 30})
    calls = []

    def process(command, **kwargs):
        calls.append(command)
        Image.new("RGB", (320, 180), "blue").save(command[-1])

    monkeypatch.setattr(worker, "process", process)
    worker.preview_contact_sheet(
        root,
        {"previews": [{"topic_id": "topic-001", "file": "previews/topic-001.mp4"}]},
    )
    assert len(calls) == 3
    assert Image.open(root / "preview-contact-sheet.jpg").size == (960, 208)


@pytest.mark.parametrize(
    "seconds, expected",
    [(0, "0:00"), (59.9, "0:59"), (305.2, "5:05"), (3599, "59:59"), (3661, "1:01:01")],
)
def test_clock_text_formats_stream_time(seconds, expected):
    assert worker.clock_text(seconds) == expected


def test_sample_label_uses_stream_time_or_candidate_relative_seconds():
    assert worker.sample_label("topic-008", 300.0, 5.2) == "topic-008 @ 5:05"
    assert worker.sample_label("topic-008", None, 5.2) == "topic-008 @ +0:05"


def test_worker_summary_passes_manifest_timings_when_present(
    previous_analysis, monkeypatch, capsys
):
    import shutil

    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)

    def gh(*args):
        if args[:2] == ("run", "view"):
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + IDENTITY,
                    "event": "workflow_dispatch",
                    "workflowName": "ClipForge audio-first stages",
                    "headBranch": "main",
                }
            )
        shutil.copytree(previous, Path(args[args.index("--dir") + 1]))
        return ""

    timings = {"clip-001": {"fetch": 1.5, "render": 20.0}, "total": 21.5}
    monkeypatch.setattr(worker, "gh", gh)
    monkeypatch.setattr(
        worker,
        "execute",
        lambda *a: {
            "status": "complete",
            "source_url": URL,
            "timings": timings,
        },
    )
    args = [
        "--stage",
        "render",
        "--previous-run",
        "123",
        "--previous-request",
        IDENTITY,
        "--request-id",
        "c" * 32,
        "--source-ref",
        "a" * 40,
        "--output",
        str(output),
    ]
    assert worker.main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["timings"] == timings
    assert summary["model_generation_requests"] == 0


SOURCE_REF = "a" * 40
CLIP_REQUESTS = {1: "1" * 32, 2: "2" * 32, 3: "3" * 32}
ANALYSIS = "d" * 64
SELECTION = "e" * 64
CACHE = "f" * 64


def edit_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    write_json(path, value)


def edit_part(root, name, change):
    """Change a receipted file and refresh its receipt, so only the named fault remains."""
    edit_json(root / name, change)
    receipt = json.loads((root / "request-receipt.json").read_text())
    receipt["sha256"][name] = digest(root / name)
    write_json(root / "request-receipt.json", receipt)


def edit_receipt(root, change):
    edit_json(root / "request-receipt.json", change)


def make_part(root, index, *, count=3, previous=IDENTITY):
    name = f"clip-{index:03}"
    clip = root / "delivery/clips" / name
    clip.mkdir(parents=True)
    (clip / "clip.mp4").write_bytes(f"synthetic {name}".encode())
    write_json(root / "asr-receipt.json", {"key": CACHE, "reused": True})
    write_json(
        root / "delivery/manifest.json",
        {"status": "clip_rendered", "rendered": []},
    )
    write_json(
        root / "manifest.json",
        {
            "status": "clip_rendered",
            "clip_index": index,
            "clip_count": count,
            "rendered": [
                {
                    "directory": "delivery/clips/" + name,
                    "source_interval": {"start": 0, "end": 1, "topic_id": "topic"},
                }
            ],
            "analysis_key": ANALYSIS,
            "selection_sha256": SELECTION,
            "timings": {
                name: {"prefetch_wait": 0.5, "fetch": 1, "render": 2},
                "total": 3.5,
            },
            "model_generation_requests": 0,
            "source_url": URL,
            "source_sha256": "9" * 64,
            "asr_reused": True,
        },
    )
    files = [
        "manifest.json",
        "asr-receipt.json",
        "delivery/manifest.json",
        f"delivery/clips/{name}/clip.mp4",
    ]
    write_json(
        root / "request-receipt.json",
        {
            "request_id": CLIP_REQUESTS[index],
            "source_ref": SOURCE_REF,
            "status": "clip_rendered",
            "clip_index": index,
            "audio_source_url": URL,
            "sha256": {n: digest(root / n) for n in files},
            "asr_reused": True,
            "audio_stage": "render",
            "previous_run_id": "123",
            "previous_request_id": previous,
        },
    )
    return root


@pytest.fixture
def merge_env(tmp_path, monkeypatch):
    settings = Settings.model_validate_json(
        Path("config.landscape.example.json").read_text()
    )
    monkeypatch.setattr(
        worker.landscape,
        "summarize_delivery",
        lambda manifest, output, expected: {
            "status": "complete",
            "rendered_clips": len(manifest["rendered"]),
        },
    )
    monkeypatch.setattr(
        worker.landscape,
        "code_identity",
        lambda settings=None: {"implementation_sha256": "x"},
    )
    monkeypatch.setattr(
        worker, "gh", lambda *a: pytest.fail("merge must not call gh with local parts")
    )
    return tmp_path, settings


def local_parts(tmp_path, order=(3, 1, 2)):
    return [
        (make_part(tmp_path / f"part-{index}", index), CLIP_REQUESTS[index])
        for index in order
    ]


def test_merge_writes_the_same_shape_as_a_whole_render(merge_env):
    tmp_path, settings = merge_env
    output = tmp_path / "out"
    result, lineage = worker.merge_parts(
        local_parts(tmp_path), output, settings, SOURCE_REF
    )
    assert result["status"] == "complete"
    assert [row["directory"] for row in result["rendered"]] == [
        "delivery/clips/clip-001",
        "delivery/clips/clip-002",
        "delivery/clips/clip-003",
    ]
    assert set(result["timings"]) == {"clip-001", "clip-002", "clip-003", "total"}
    assert result["timings"]["total"] == pytest.approx(10.5)
    assert result["timings"]["clip-002"] == {
        "prefetch_wait": 0.5,
        "fetch": 1,
        "render": 2,
    }
    assert json.loads((output / "manifest.json").read_text()) == result
    delivery = json.loads((output / "delivery/manifest.json").read_text())
    assert set(delivery) == {
        "status",
        "rendered",
        "selection_receipt",
        "analysis_key",
        "selection_sha256",
        "video_review_attested",
        "human_review_performed",
        "transcript_cache",
        "timings",
    }
    assert [row["directory"] for row in delivery["rendered"]] == [
        "clips/clip-001",
        "clips/clip-002",
        "clips/clip-003",
    ]
    assert (output / "delivery/clips/clip-002/clip.mp4").read_bytes() == (
        b"synthetic clip-002"
    )
    assert (
        json.loads((output / "delivery/e2e-summary.json").read_text())["rendered_clips"]
        == 3
    )
    assert lineage == {
        "previous_request_id": IDENTITY,
        "part_request_ids": sorted(CLIP_REQUESTS.values()),
    }


def replace_with_symlink(root):
    clip = root / "delivery/clips/clip-002/clip.mp4"
    real = root / "elsewhere.mp4"
    real.write_bytes(clip.read_bytes())
    clip.unlink()
    clip.symlink_to(real)


FAULTS = {
    "missing": lambda tmp, parts: parts.pop(),
    "duplicate": lambda tmp, parts: parts.append(
        (make_part(tmp / "again", 1), CLIP_REQUESTS[1])
    ),
    "previous_request": lambda tmp, parts: edit_receipt(
        parts[0][0], lambda r: r.update(previous_request_id="9" * 32)
    ),
    "analysis": lambda tmp, parts: edit_part(
        parts[0][0], "manifest.json", lambda m: m.update(analysis_key="other")
    ),
    "clip_count": lambda tmp, parts: edit_part(
        parts[0][0], "manifest.json", lambda m: m.update(clip_count=4)
    ),
    "source_ref": lambda tmp, parts: edit_receipt(
        parts[0][0], lambda r: r.update(source_ref="b" * 40)
    ),
    "hash": lambda tmp, parts: (
        parts[0][0] / "delivery/clips/clip-003/clip.mp4"
    ).write_bytes(b"changed"),
    "traversal": lambda tmp, parts: edit_receipt(
        parts[0][0], lambda r: r["sha256"].update({"../escape": "a" * 64})
    ),
    "absolute": lambda tmp, parts: edit_receipt(
        parts[0][0], lambda r: r["sha256"].update({"/etc/passwd": "a" * 64})
    ),
    "symlink": lambda tmp, parts: replace_with_symlink(parts[2][0]),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_merge_refuses_missing_duplicate_mismatched_or_unsafe_parts(merge_env, fault):
    tmp_path, settings = merge_env
    parts = local_parts(tmp_path)
    FAULTS[fault](tmp_path, parts)
    with pytest.raises(PipelineError):
        worker.merge_parts(parts, tmp_path / "out", settings, SOURCE_REF)
    assert not (tmp_path / "out/manifest.json").exists()


def test_merge_refuses_to_overwrite_an_existing_delivery(merge_env):
    tmp_path, settings = merge_env
    output = tmp_path / "out"
    (output / "delivery").mkdir(parents=True)
    with pytest.raises(PipelineError, match="already contains"):
        worker.merge_parts(local_parts(tmp_path), output, settings, SOURCE_REF)


def test_merge_main_fetches_only_successful_clip_runs_for_their_request(
    tmp_path, monkeypatch, capsys
):
    import shutil

    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    sources = {
        CLIP_REQUESTS[index]: make_part(tmp_path / f"src-{index}", index)
        for index in (1, 2, 3)
    }
    runs = {"101": CLIP_REQUESTS[1], "102": CLIP_REQUESTS[2], "103": CLIP_REQUESTS[3]}
    calls = []

    def gh(*args):
        calls.append(args)
        if args[:2] == ("run", "view"):
            request = runs[args[2]]
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + request,
                    "event": "workflow_dispatch",
                    "workflowName": "ClipForge audio-first stages",
                    "headBranch": "main",
                }
            )
        request = args[args.index("--name") + 1].removeprefix("clipforge-result-")
        shutil.copytree(sources[request], Path(args[args.index("--dir") + 1]))
        return ""

    monkeypatch.setattr(worker, "gh", gh)
    monkeypatch.setattr(
        worker.landscape,
        "summarize_delivery",
        lambda manifest, output, expected: {"status": "complete"},
    )
    monkeypatch.setattr(
        worker.landscape,
        "code_identity",
        lambda settings=None: {"implementation_sha256": "x"},
    )
    output = tmp_path / "out"
    part_runs = ",".join(f"{run}:{request}" for run, request in runs.items())
    args = [
        "--stage",
        "merge",
        "--part-runs",
        part_runs,
        "--request-id",
        "c" * 32,
        "--source-ref",
        SOURCE_REF,
        "--output",
        str(output),
    ]
    assert worker.main(args) == 0
    assert {c[2] for c in calls if c[:2] == ("run", "view")} == set(runs)
    receipt = json.loads((output / "request-receipt.json").read_text())
    assert receipt["audio_stage"] == "merge"
    assert receipt["status"] == "complete"
    assert receipt["part_request_ids"] == sorted(CLIP_REQUESTS.values())
    assert receipt["previous_request_id"] == IDENTITY
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


@pytest.mark.parametrize(
    "field, value",
    [
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("displayTitle", "ClipForge x"),
        ("event", "push"),
        ("event", None),
        ("workflowName", "Unrelated workflow"),
        ("workflowName", None),
        ("headBranch", "unreviewed"),
        ("headBranch", "ccr-12fe9485-ugaszt"),
        ("headBranch", None),
    ],
)
def test_merge_main_refuses_a_clip_run_that_is_not_its_successful_render(
    tmp_path, monkeypatch, field, value
):
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    run = {
        "status": "completed",
        "conclusion": "success",
        "displayTitle": "ClipForge " + CLIP_REQUESTS[1],
        "event": "workflow_dispatch",
        "workflowName": "ClipForge audio-first stages",
        "headBranch": "main",
    }
    run[field] = value
    downloads = []

    def gh(*args):
        if args[:2] == ("run", "view"):
            return json.dumps(run)
        downloads.append(args)
        return ""

    monkeypatch.setattr(worker, "gh", gh)
    args = [
        "--stage",
        "merge",
        "--part-runs",
        f"101:{CLIP_REQUESTS[1]}",
        "--request-id",
        "c" * 32,
        "--source-ref",
        SOURCE_REF,
        "--output",
        str(tmp_path / "out"),
    ]
    assert worker.main(args) == 1
    assert downloads == []


def test_render_stage_passes_one_clip_index_and_records_it(
    previous_analysis, monkeypatch
):
    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    seen = []

    def gh(*args):
        if args[:2] == ("run", "view"):
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + IDENTITY,
                    "event": "workflow_dispatch",
                    "workflowName": "ClipForge audio-first stages",
                    "headBranch": "main",
                }
            )
        import shutil

        shutil.copytree(previous, Path(args[args.index("--dir") + 1]))
        return ""

    def execute(*args):
        seen.append(args)
        return {"status": "clip_rendered", "source_url": URL, "clip_index": 2}

    monkeypatch.setattr(worker, "gh", gh)
    monkeypatch.setattr(worker, "execute", execute)
    args = [
        "--stage",
        "render",
        "--previous-run",
        "123",
        "--previous-request",
        IDENTITY,
        "--request-id",
        "c" * 32,
        "--source-ref",
        SOURCE_REF,
        "--output",
        str(output),
        "--clip-index",
        "2",
        "--review-file",
        str(previous / "review.json"),
    ]
    assert worker.main(args) == 0
    assert seen[0][-1] == 2
    receipt = json.loads((output / "request-receipt.json").read_text())
    assert receipt["status"] == "clip_rendered" and receipt["clip_index"] == 2


@pytest.mark.parametrize("clip_index", ["0", "21"])
def test_clip_index_is_limited_to_one_to_twenty(clip_index):
    with pytest.raises(SystemExit):
        worker.main(["--stage", "render", "--clip-index", clip_index])


def test_single_clip_retains_only_merge_inputs_and_consistent_hashes(
    previous_analysis, monkeypatch
):
    previous, output, settings = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    monkeypatch.setattr(worker, "gh", _previous_gh(previous))
    monkeypatch.setattr(
        worker, "KickVOD", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )

    def render(vod, root, settings, review, analysis_result=None, only=None):
        assert only == 2
        for directory in ("ranges", "previews"):
            (root / directory).mkdir(exist_ok=True)
            (root / directory / "synthetic.mp4").write_bytes(b"intermediate")
            (root / directory / "synthetic.mp4.receipt.json").write_text("{}")
        (root / "previews.json").write_text("{}")
        clip = root / "delivery/clips/clip-002"
        clip.mkdir(parents=True)
        for name in ("video.mp4", "captions.ass", "captions.srt", "quality.json"):
            (clip / name).write_bytes(b"synthetic delivery")
        manifest = {
            "status": "clip_rendered",
            "clip_index": 2,
            "clip_count": settings.clips,
            "analysis_key": ANALYSIS,
            "selection_sha256": SELECTION,
            "timings": {"clip-002": {"render": 1.0}, "total": 1.0},
            "rendered": [
                {
                    "directory": "clips/clip-002",
                    "sha256": {p.name: digest(p) for p in clip.iterdir()},
                }
            ],
        }
        write_json(root / "delivery/manifest.json", manifest)

    monkeypatch.setattr(audio_first, "render", render)
    args = _previews_args(output)
    args[1] = "render"
    args += ["--clip-index", "2", "--review-file", str(previous / "review.json")]
    assert worker.main(args) == 0
    for name in (
        "ranges",
        "audio.m4a",
        "previews",
        "transcripts",
        "topics.json",
        "previews.json",
        ".state",
    ):
        assert not (output / name).exists()
    assert (output / "asr-receipt.json").is_file()
    receipt = json.loads((output / "request-receipt.json").read_text())
    files = {
        p.relative_to(output).as_posix()
        for p in output.rglob("*")
        if p.is_file()
        and not p.name.startswith(".")
        and p.name != "request-receipt.json"
    }
    assert set(receipt["sha256"]) == files
    assert all(digest(output / name) == sha for name, sha in receipt["sha256"].items())
    for manifest_name in ("manifest.json", "delivery/manifest.json"):
        manifest = json.loads((output / manifest_name).read_text())
        root = output if manifest_name == "manifest.json" else output / "delivery"
        for row in manifest["rendered"]:
            for name, sha in row["sha256"].items():
                assert digest(root / row["directory"] / name) == sha
    loaded = worker.load_part(output, "c" * 32, SOURCE_REF)
    assert loaded["index"] == 2
    assert loaded["identity"]["transcript_key"]


def _previous_gh(previous):
    import shutil

    def gh(*args):
        if args[:2] == ("run", "view"):
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + IDENTITY,
                    "event": "workflow_dispatch",
                    "workflowName": "ClipForge audio-first stages",
                    "headBranch": "main",
                }
            )
        shutil.copytree(previous, Path(args[args.index("--dir") + 1]))
        return ""

    return gh


def _previews_args(output):
    return [
        "--stage",
        "previews",
        "--previous-run",
        "123",
        "--previous-request",
        IDENTITY,
        "--request-id",
        "c" * 32,
        "--source-ref",
        "a" * 40,
        "--output",
        str(output),
    ]


@pytest.mark.parametrize(
    "field, value",
    [
        ("event", "push"),
        ("event", None),
        ("workflowName", "Unrelated workflow"),
        ("workflowName", None),
        ("headBranch", "unreviewed"),
        ("headBranch", "ccr-12fe9485-ugaszt"),
        ("headBranch", None),
    ],
)
def test_previous_run_provenance_is_checked_before_downloading(
    tmp_path, monkeypatch, field, value
):
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    calls = []
    run = {
        "status": "completed",
        "conclusion": "success",
        "displayTitle": "ClipForge " + IDENTITY,
        "event": "workflow_dispatch",
        "workflowName": "ClipForge audio-first stages",
        "headBranch": "main",
        field: value,
    }

    def gh(*args):
        calls.append(args)
        assert args[:2] == ("run", "view"), "unverified run was downloaded"
        assert args[-2:] == ("--json", worker.RUN_FIELDS)
        return json.dumps(run)

    monkeypatch.setattr(worker, "gh", gh)
    assert worker.main(_previews_args(tmp_path / "out")) == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "workflow, branch",
    [
        ("ClipForge audio-first stages", "main"),
        ("ClipForge", "main"),
    ],
)
def test_download_parts_accepts_only_confirmed_stage_metadata(
    tmp_path, monkeypatch, workflow, branch
):
    calls = []

    def gh(*args):
        calls.append(args)
        if args[:2] == ("run", "view"):
            assert args[-2:] == ("--json", worker.RUN_FIELDS)
            return json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "ClipForge " + IDENTITY,
                    "event": "workflow_dispatch",
                    "workflowName": workflow,
                    "headBranch": branch,
                }
            )
        assert args[:2] == ("run", "download")
        return ""

    monkeypatch.setattr(worker, "gh", gh)
    assert worker.download_parts(f"123:{IDENTITY}", tmp_path) == [
        (tmp_path / IDENTITY, IDENTITY)
    ]
    assert len(calls) == 2


def test_pipeline_error_is_recorded_with_its_own_fixed_text(
    previous_analysis, monkeypatch, capsys
):
    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    monkeypatch.setattr(worker, "gh", _previous_gh(previous))
    text = "clip failed the quality gate: captions.srt and captions.ass differ in timing or text"

    def execute(*args):
        raise PipelineError(text)

    monkeypatch.setattr(worker, "execute", execute)
    assert worker.main(_previews_args(output)) == 1
    record = json.loads((output / "stage-error.json").read_text())
    assert record == {
        "stage": "previews",
        "error_type": "PipelineError",
        "message": text,
    }
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "Private audio stage failed: PipelineError: " + text
    assert "No ASR or model fallback" in lines[1]


def test_non_pipeline_error_records_only_its_type(
    previous_analysis, monkeypatch, capsys
):
    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    monkeypatch.setattr(worker, "gh", _previous_gh(previous))
    leaked = "https://kick.com/example/videos/secret-token /home/user/private.m4a"

    def execute(*args):
        raise OSError(leaked)

    monkeypatch.setattr(worker, "execute", execute)
    assert worker.main(_previews_args(output)) == 1
    record = json.loads((output / "stage-error.json").read_text())
    assert record == {"stage": "previews", "error_type": "OSError", "message": ""}
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "Private audio stage failed: OSError"
    for private in ("secret-token", "kick.com", "/home/user", "private.m4a"):
        assert private not in err
        assert private not in (output / "stage-error.json").read_text()


def same_run_args(previous, output, monkeypatch):
    for key, value in {
        "GITHUB_REPOSITORY": worker.REPO,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW": "ClipForge",
        "GITHUB_JOB": "previews",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": "123",
        "REQUEST_ID": IDENTITY,
        "CLIPFORGE_SOURCE_REF": SOURCE_REF,
    }.items():
        monkeypatch.setenv(key, value)
    edit_receipt(
        previous, lambda r: r.update(source_ref=SOURCE_REF, workflow_run="123")
    )
    monkeypatch.setattr(
        worker, "gh", lambda *a: pytest.fail("same-run path queried an unfinished run")
    )
    args = _previews_args(output)
    args[args.index("--request-id") + 1] = IDENTITY
    return args + ["--same-run-analysis", str(previous)]


@pytest.mark.parametrize(
    "fault",
    [
        "run",
        "request",
        "source_ref",
        "receipt_run",
        "receipt_request",
        "receipt_source",
        "hash",
        "stage",
        "event",
        "workflow",
        "job",
        "branch",
    ],
)
def test_same_run_path_refuses_unrelated_or_modified_analysis(
    previous_analysis, monkeypatch, fault
):
    previous, output, _ = previous_analysis
    args = same_run_args(previous, output, monkeypatch)
    if fault in {"run", "request", "source_ref", "stage"}:
        flag, value = {
            "run": ("--previous-run", "456"),
            "request": ("--previous-request", "f" * 32),
            "source_ref": ("--source-ref", "f" * 40),
            "stage": ("--stage", "render"),
        }[fault]
        args[args.index(flag) + 1] = value
    elif fault.startswith("receipt_"):
        field, value = {
            "receipt_run": ("workflow_run", "456"),
            "receipt_request": ("request_id", "f" * 32),
            "receipt_source": ("source_ref", "f" * 40),
        }[fault]
        edit_receipt(previous, lambda r: r.update({field: value}))
    elif fault == "hash":
        (previous / "audio.m4a").write_bytes(b"modified synthetic audio")
    else:
        field, value = {
            "event": ("GITHUB_EVENT_NAME", "push"),
            "workflow": ("GITHUB_WORKFLOW", "Unrelated workflow"),
            "job": ("GITHUB_JOB", "colab-auto"),
            "branch": ("GITHUB_REF", "refs/heads/unreviewed"),
        }[fault]
        monkeypatch.setenv(field, value)
    monkeypatch.setattr(
        worker,
        "execute",
        lambda *a: pytest.fail("unverified analysis reached execution"),
    )
    assert worker.main(args) == 1


def test_render_reuses_chained_clipforge_run_on_main(previous_analysis, monkeypatch):
    previous, output, _ = previous_analysis
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    original = _previous_gh(previous)
    downloads = []

    def gh(*args):
        value = original(*args)
        if args[:2] == ("run", "view"):
            run = json.loads(value)
            run.update(workflowName="ClipForge", headBranch="main")
            return json.dumps(run)
        downloads.append(args)
        return value

    edit_receipt(previous, lambda r: r.update(status="awaiting_video_review"))
    edit_json(
        previous / "manifest.json", lambda m: m.update(status="awaiting_video_review")
    )
    edit_receipt(
        previous,
        lambda r: r["sha256"].update(
            {"manifest.json": digest(previous / "manifest.json")}
        ),
    )
    monkeypatch.setattr(worker, "gh", gh)

    def execute(previous, output, previous_id, settings, mode, *args):
        assert mode == "render"
        output.mkdir()
        assert worker.restore_analysis(previous, output, previous_id) == URL
        return {"status": "complete", "source_url": URL}

    monkeypatch.setattr(worker, "execute", execute)
    args = _previews_args(output)
    args[1] = "render"
    assert worker.main(args) == 0
    assert (
        downloads[0][downloads[0].index("--name") + 1] == "clipforge-result-" + IDENTITY
    )


CHAT_CONFIG = "config.landscape-chat.example.json"


def test_merge_refuses_parts_rendered_with_different_configurations(merge_env):
    tmp_path, settings = merge_env
    parts = local_parts(tmp_path)
    edit_receipt(parts[0][0], lambda value: value.update(config_file=CHAT_CONFIG))
    with pytest.raises(PipelineError, match="different configuration files"):
        worker.merge_parts(parts, tmp_path / "out", settings, SOURCE_REF)
    assert not (tmp_path / "out").exists()


def test_merge_requires_the_configuration_its_parts_used(merge_env):
    tmp_path, settings = merge_env
    with pytest.raises(PipelineError, match="another configuration file"):
        worker.merge_parts(
            local_parts(tmp_path),
            tmp_path / "out",
            settings,
            SOURCE_REF,
            config_file=CHAT_CONFIG,
        )
    assert not (tmp_path / "out").exists()


def test_receipt_configuration_name_defaults_and_must_be_a_config_file(merge_env):
    tmp_path, settings = merge_env
    parts = local_parts(tmp_path)
    assert worker.receipt_config_file(parts[0][0]) == "config.landscape.example.json"
    edit_receipt(parts[0][0], lambda value: value.update(config_file="../x.json"))
    with pytest.raises(PipelineError, match="configuration is invalid"):
        worker.receipt_config_file(parts[0][0])


def test_write_receipt_records_the_configuration_name(tmp_path):
    output = tmp_path / "out"
    (output / "delivery").mkdir(parents=True)
    (output / "delivery/manifest.json").write_text("{}")
    result = {
        "status": "complete",
        "source_url": "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5",
    }
    worker.write_receipt(
        output,
        result,
        {"previous_request_id": IDENTITY},
        "e" * 32,
        SOURCE_REF,
        "previews",
        "7",
        config_file=CHAT_CONFIG,
    )
    receipt = json.loads((output / "request-receipt.json").read_text())
    assert receipt["config_file"] == CHAT_CONFIG


def test_main_rejects_a_configuration_outside_the_repository_root(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_REPOSITORY", worker.REPO)
    code = worker.main(
        [
            "--stage",
            "previews",
            "--previous-run",
            "7",
            "--previous-request",
            IDENTITY,
            "--request-id",
            "e" * 32,
            "--source-ref",
            SOURCE_REF,
            "--output",
            str(tmp_path / "out"),
            "--config",
            "sub/config.landscape.example.json",
        ]
    )
    assert code == 1
    assert "invalid private worker identity" in capsys.readouterr().err
