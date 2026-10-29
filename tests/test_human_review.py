"""Offline checks for the review packet and approval boundary."""

import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from clipforge import review_package, release_transfer
from clipforge import audio_understanding
from clipforge.models import SelectedClip
from clipforge.storage import PipelineError


def example():
    clip = {
        "start": 10,
        "end": 30,
        "title": "<話題>",
        "speaker_turns": [
            {"start": 0, "end": 2, "speaker_id": "speaker_1"},
            {"start": 2, "end": 4, "speaker_id": "speaker_2"},
        ],
    }
    transcript = {
        "segments": [
            {"start": 0, "end": 2, "text": "元の語"},
            {"start": 2, "end": 4, "text": "変更 20円"},
        ]
    }
    raw = {
        "segments": [
            {"start": 0, "end": 2.1, "text": "元の語"},
            {"start": 1.9, "end": 4, "text": "旧語 20円"},
        ]
    }
    provenance = {
        "segments": [
            {"start": 0, "end": 2, "text": "元の語", "source_ids": [1]},
            {"start": 2, "end": 4, "text": "変更 20円", "source_ids": [2]},
        ]
    }
    return clip, transcript, raw, provenance


def test_comparison_uses_actual_source_ids_not_overlapping_source_times():
    clip, transcript, raw, provenance = example()
    first, second = review_package.review_rows(clip, transcript, raw, provenance)
    assert first["original"] == "元の語"
    assert not any("語が変わった" in reason for reason in first["reasons"])
    assert second["original"] == "旧語 20円"
    assert "清書で語が変わった可能性" in second["reasons"]
    assert "別の話者の可能性" in second["reasons"]
    assert "数字・数量の可能性" in second["reasons"]


def test_unmatched_provenance_is_never_claimed_unchanged():
    clip, transcript, raw, provenance = example()
    provenance["segments"][0]["source_ids"] = [999]
    first, _ = review_package.review_rows(clip, transcript, raw, provenance)
    assert not first["mapping_exact"]
    assert any("対応不明" in reason for reason in first["reasons"])


def test_cleanup_keeps_ids_of_accepted_and_preserved_source_rows():
    class Client:
        def generate(self, *args, **kwargs):
            return {
                "audio_access": "heard",
                "segments": [{"source_ids": [2], "text": "修正"}],
            }, None

    raw = {
        "segments": [
            {"start": 1, "end": 2, "text": "保持", "speaker": "a"},
            {"start": 3, "end": 4, "text": "元", "speaker": "a"},
        ]
    }
    clip = SelectedClip(
        start=0,
        end=10,
        candidate_id="topic",
        title="話題",
        reason="理由",
        confidence=0.9,
    )
    result = audio_understanding.clean_selected_clip(Client(), raw, 1, clip, [], None)
    assert [(row["text"], row["source_ids"]) for row in result["segments"]] == [
        ("保持", [1]),
        ("修正", [2]),
    ]


def test_review_page_escapes_content_and_offers_sample_and_seek():
    clip, transcript, raw, provenance = example()
    page = review_package.make_page(
        {"clips": [clip]}, {"clips": [transcript]}, [raw], [provenance]
    )
    assert "&lt;話題&gt;" in page
    assert 'src="clip-001.mp4"' in page
    assert "変更なしの抜き取り（1行）" in page
    assert "清書で語が変わった可能性" in page
    assert "function seek" in page


def test_package_uses_each_selected_interval_without_render_or_model(
    tmp_path, monkeypatch
):
    clip, transcript, raw, provenance = example()
    selection = {
        "selection_basis": "audio_understanding",
        "source_url": "https://example.test/video.mp4",
        "clips": [clip],
    }
    for name, value in (
        ("final-selection.json", selection),
        ("selected-transcripts.json", {"clips": [transcript]}),
        ("transcribe-selected-001.json", raw),
        ("review-source-001.json", provenance),
    ):
        (tmp_path / name).write_text(json.dumps(value))
    config = tmp_path / "config.json"
    config.write_text("{}")
    calls = []
    monkeypatch.setattr(review_package, "open_source", lambda *a, **kw: object())

    def fake_retrieve(vod, path, start, end, quality):
        calls.append((start, end, quality))
        path.write_bytes(b"fake video")

    monkeypatch.setattr(review_package, "retrieved", fake_retrieve)
    output = tmp_path / "package.zip"
    review_package.create(tmp_path, output, config)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "review.html",
            "clip-001.mp4",
            "final-selection.json",
            "selected-transcripts.json",
        }
        assert archive.read("clip-001.mp4") == b"fake video"
    assert calls == [(10, 30, "preview")]


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe are required",
)
def test_package_contains_playable_selected_video_with_audio(tmp_path, monkeypatch):
    from clipforge.media_sources import RemoteMedia

    source = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        RemoteMedia,
        "_download",
        lambda self, audio_only, quality: (
            source,
            {"duration": 4, "has_video": True, "height": 90, "video_start": 0},
        ),
    )
    clip, transcript, raw, provenance = example()
    clip.update(start=1, end=3)
    selection = {
        "selection_basis": "audio_understanding",
        "source_url": "https://example.test/video.mp4",
        "clips": [clip],
    }
    for name, value in (
        ("final-selection.json", selection),
        ("selected-transcripts.json", {"clips": [transcript]}),
        ("transcribe-selected-001.json", raw),
        ("review-source-001.json", provenance),
    ):
        (tmp_path / name).write_text(json.dumps(value))
    config = tmp_path / "config.json"
    config.write_text("{}")
    package = tmp_path / "review.zip"
    review_package.create(tmp_path, package, config)
    with zipfile.ZipFile(package) as bundle:
        video = tmp_path / "clip.mp4"
        video.write_bytes(bundle.read("clip-001.mp4"))
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = json.loads(result.stdout)
    assert {row["codec_type"] for row in probe["streams"]} >= {"audio", "video"}
    assert 1.8 <= float(probe["format"]["duration"]) <= 2.3


def test_approval_rejects_modified_package(tmp_path):
    path = tmp_path / "review-package.zip"
    selection = b'{"source_url":"https://example.test/video.mp4","selection_basis":"audio_understanding","clips":[{}]}'
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("final-selection.json", selection)
        bundle.writestr("selected-transcripts.json", b'{"clips":[{}]}')
        bundle.writestr("review.html", b"review")
        bundle.writestr("clip-001.mp4", b"video")
    contents = path.read_bytes()
    state = {
        "source_url": "https://example.test/video.mp4",
        "review_package": {
            "size": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
        },
    }
    hashes = review_package.validate_local_package(
        state, tmp_path / "production-state.json"
    )
    assert hashes["final-selection.json"] == hashlib.sha256(selection).hexdigest()
    path.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
    with pytest.raises(PipelineError, match="changed"):
        review_package.validate_local_package(state, tmp_path / "production-state.json")


def test_review_archive_rejects_wrong_source_and_unexpected_member(tmp_path):
    path = tmp_path / "review.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(
            "final-selection.json",
            '{"source_url":"https://example.test/other.mp4","selection_basis":"audio_understanding","clips":[{}]}',
        )
        bundle.writestr("selected-transcripts.json", '{"clips":[{}]}')
        bundle.writestr("review.html", "review")
        bundle.writestr("clip-001.mp4", "video")
        bundle.writestr("../escape", "unsafe")
    with pytest.raises(PipelineError, match="another source"):
        review_package.inspect_archive(path, "https://example.test/video.mp4")
    with pytest.raises(PipelineError, match="contents"):
        review_package.inspect_archive(path, "https://example.test/other.mp4")


def test_render_cannot_dispatch_without_review_approval(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    producer = importlib.import_module("produce_with_gemini")
    monkeypatch.setattr(
        producer, "gh", lambda *a, **kw: pytest.fail("dispatch forbidden")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "produce_with_gemini",
            "https://example.test/video.mp4",
            "--output",
            str(tmp_path / "out"),
            "--source-ref",
            "a" * 40,
            "--clips",
            "1",
            "--render-profile",
            "preview",
            "--render-only",
        ],
    )
    with pytest.raises(SystemExit):
        producer.main()
    assert not (tmp_path / "out").exists()


def test_approved_render_dispatch_carries_reviewed_content_hashes(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    producer = importlib.import_module("produce_with_gemini")
    folder = tmp_path / "review"
    folder.mkdir()
    package = folder / "review-package.zip"
    selection = b'{"source_url":"https://example.test/video.mp4","selection_basis":"audio_understanding","clips":[{}]}'
    transcripts = b'{"clips":[{}]}'
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("final-selection.json", selection)
        bundle.writestr("selected-transcripts.json", transcripts)
        bundle.writestr("review.html", b"review")
        bundle.writestr("clip-001.mp4", b"video")
    state = {
        "status": "awaiting_review",
        "editorial_release": "clipforge-kick-editorial-123",
        "source_url": "https://example.test/video.mp4",
        "review_run_id": "123",
        "review_request_id": "a" * 32,
        "review_package": {
            "size": package.stat().st_size,
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        },
    }
    (folder / "production-state.json").write_text(json.dumps(state))
    sent = []

    class StoppedBeforeDispatch(Exception):
        pass

    def fake_gh(*args, body=None):
        sent.append(json.loads(body))
        raise StoppedBeforeDispatch()

    monkeypatch.setattr(producer, "gh", fake_gh)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "produce_with_gemini",
            state["source_url"],
            "--output",
            str(tmp_path / "render"),
            "--source-ref",
            "b" * 40,
            "--clips",
            "1",
            "--render-profile",
            "preview",
            "--render-only",
            "--review-approved",
            "--review-state",
            str(folder / "production-state.json"),
            "--editorial-release",
            state["editorial_release"],
            "--analysis-run",
            "123",
            "--analysis-request",
            "a" * 32,
        ],
    )
    with pytest.raises(StoppedBeforeDispatch):
        producer.main()
    assert (
        sent[0]["inputs"]["approved_selection_sha256"]
        == hashlib.sha256(selection).hexdigest()
    )
    assert (
        sent[0]["inputs"]["approved_transcripts_sha256"]
        == hashlib.sha256(transcripts).hexdigest()
    )
    assert sent[0]["inputs"]["review_approved"] is True


def test_rest_asset_download_checks_size_and_never_keeps_partial(tmp_path, monkeypatch):
    record = {"assets": [{"name": "transfer.json", "id": 7, "size": 3}]}
    called = []

    def fake_run(args, stdout, **kwargs):
        called.append(args)
        stdout.write(b"abc")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(release_transfer.subprocess, "run", fake_run)
    destination = tmp_path / "transfer.json"
    release_transfer.download_release_asset(record, "transfer.json", destination)
    assert destination.read_bytes() == b"abc"
    assert called[0][:2] == ["gh", "api"]
    assert "Accept: application/octet-stream" in called[0]
    record["assets"][0]["size"] = 4
    with pytest.raises(release_transfer.TransferError, match="RESULT_SIZE_MISMATCH"):
        release_transfer.download_release_asset(
            record, "transfer.json", tmp_path / "partial"
        )
    assert not (tmp_path / "partial").exists()
    with pytest.raises(FileExistsError):
        release_transfer.download_release_asset(record, "transfer.json", destination)
    assert destination.read_bytes() == b"abc"


def test_editorial_rest_download_validates_privacy_and_size(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    module = importlib.import_module("download_editorial")
    monkeypatch.setattr(
        module,
        "api",
        lambda path: {"private": True}
        if path == f"repos/{module.REPO}"
        else {"assets": [{"id": 4, "name": "final-selection.json", "size": 2}]},
    )

    def fake_run(args, stdout, **kwargs):
        stdout.write(b"{}")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.download("clipforge-kick-editorial-123", tmp_path, ["final-selection.json"])
    assert (tmp_path / "final-selection.json").read_bytes() == b"{}"
    monkeypatch.setattr(module, "api", lambda path: {"private": False})
    with pytest.raises(RuntimeError, match="private"):
        module.download(
            "clipforge-kick-editorial-123",
            tmp_path / "another",
            ["final-selection.json"],
        )


def test_workflow_orders_review_and_checks_before_gpu():
    root = Path(__file__).resolve().parents[1]
    workflow = json.loads((root / ".github/workflows/clipforge-stages.yml").read_text())
    assert workflow == json.loads((root / "deploy/audio-stages.yml").read_text())
    stages = workflow["jobs"]["audio-understanding"]["steps"]
    assert (
        next(i for i, s in enumerate(stages) if s.get("id") == "process")
        < next(
            i
            for i, s in enumerate(stages)
            if s.get("name") == "Prepare private topic videos and review checklist"
        )
        < next(i for i, s in enumerate(stages) if s.get("id") == "save-editorial")
    )
    steps = workflow["jobs"]["colab-render"]["steps"]
    names = [step.get("name") for step in steps]
    assert (
        names.index("Require explicit review approval for rendering")
        < names.index("Load saved selected-only subtitles for render-only continuation")
        < names.index("Validate the clip render request")
        < names.index("Allocate the cheapest runtime the account grants")
    )
    assert (
        workflow["on"]["workflow_dispatch"]["inputs"]["review_approved"]["default"]
        is False
    )
    validation = steps[names.index("Validate the clip render request")]["run"]
    assert "selected-transcripts.json" in validation
    assert "CLIPFORGE_APPROVED_TRANSCRIPTS_SHA256" in validation
    assert (
        "Approved rendering requires the Colab editorial route"
        in workflow["jobs"]["audio-stage"]["steps"][0]["run"]
    )


def test_controller_rejects_selection_mismatch_before_allocation(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    control = importlib.import_module("colab_actions")
    review = tmp_path / "review.json"
    review.write_text('{"clips": []}')
    for key, value in {
        "AUDIO_STAGE": "render",
        "PREVIOUS_RUN": "123",
        "PREVIOUS_REQUEST": "a" * 32,
        "REVIEW_FILE": str(review),
        "CLIPFORGE_REQUIRE_REVIEW_HASHES": "true",
        "CLIPFORGE_APPROVED_SELECTION_SHA256": "b" * 64,
        "CLIPFORGE_APPROVED_TRANSCRIPTS_SHA256": "c" * 64,
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(control.ControlError, match="does not match"):
        control.stage_request()
    monkeypatch.setenv(
        "CLIPFORGE_APPROVED_SELECTION_SHA256",
        hashlib.sha256(review.read_bytes()).hexdigest(),
    )
    assert control.stage_request()["approved_transcripts_sha256"] == "c" * 64


def test_workflow_validation_rejects_changed_subtitles(tmp_path):
    root = Path(__file__).resolve().parents[1]
    workflow = json.loads((root / ".github/workflows/clipforge-stages.yml").read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["colab-render"]["steps"]
        if step.get("name") == "Validate the clip render request"
    )
    directory = tmp_path / "editorial-render"
    directory.mkdir()
    selection = b'{"clips":[]}'
    transcript = b'{"clips":[1]}'
    (directory / "final-selection.json").write_bytes(selection)
    (directory / "selected-transcripts.json").write_bytes(transcript)
    (tmp_path / "review.json").write_bytes(selection)
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "AUDIO_STAGE": "render",
        "CLIPFORGE_SOURCE_REF": "a" * 40,
        "REQUEST_ID": "b" * 32,
        "PREVIOUS_REQUEST": "c" * 32,
        "INPUT_TAG": "clipforge-input-" + "b" * 32,
        "PREVIOUS_RUN": "123",
        "CLIP_INDEX": "",
        "CONFIG_FILE": "config.landscape-long-preview.example.json",
        "CLIPFORGE_AUDIO_REVIEW_RELEASE": "clipforge-kick-editorial-123",
        "CLIPFORGE_APPROVED_SELECTION_SHA256": hashlib.sha256(selection).hexdigest(),
        "CLIPFORGE_APPROVED_TRANSCRIPTS_SHA256": hashlib.sha256(transcript).hexdigest(),
    }
    assert (
        subprocess.run(
            ["bash", "-e", "-c", script], env=env, capture_output=True
        ).returncode
        == 0
    )
    (directory / "selected-transcripts.json").write_bytes(b'{"clips":[2]}')
    assert (
        subprocess.run(
            ["bash", "-e", "-c", script], env=env, capture_output=True
        ).returncode
        != 0
    )


def test_worker_rejects_changed_transcript_before_render(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    worker = importlib.import_module("colab_remote_job")
    monkeypatch.setattr(worker, "WORK", tmp_path)
    calls = []
    monkeypatch.setattr(
        worker, "command", lambda command, *args, **kwargs: calls.append(command)
    )
    selection = b'{"selection_basis":"audio_understanding","source_url":"https://example.test/video.mp4","clips":[]}'
    files = {
        "audio-editor-receipt.json": b'{"status":"complete"}',
        "final-selection.json": selection,
        "selected-transcripts.json": b'{"clips":["changed"]}',
    }

    class Bundles:
        def __init__(self, token):
            pass

        def json_request(self, method, url):
            return {"assets": [{"name": name} for name in files]}

        def download_asset(self, asset, destination, limit):
            destination.write_bytes(files[asset["name"]])

    monkeypatch.setattr(worker, "ReleaseBundles", Bundles)
    payload = {
        "github_token": "fake",
        "audio_review_release": "clipforge-kick-editorial-123",
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "stage": {
            "review": selection.decode(),
            "previous_run": "123",
            "previous_request": "c" * 32,
            "approved_selection_sha256": hashlib.sha256(selection).hexdigest(),
            "approved_transcripts_sha256": "d" * 64,
        },
    }
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(RuntimeError, match="subtitles differ"):
        worker.render_stage(payload, output, {}, {}, {})
    assert len(calls) == 1  # setup only; no render command
