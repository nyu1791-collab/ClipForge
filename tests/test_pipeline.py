import json

import pytest

from clipforge import media, pipeline, selection, signals
from clipforge.models import SelectedClip
from clipforge.storage import PipelineError, digest, write_json


@pytest.fixture
def job(tmp_path, monkeypatch, transcript, on_target_loudness):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"synthetic-input" * 200)
    output = tmp_path / "out"
    counts = {"asr": 0, "scenes": 0, "generate": 0, "render": 0, "preview": 0}

    def asr(*args):
        counts["asr"] += 1
        return transcript.model_dump()

    def scenes(*args):
        counts["scenes"] += 1
        return {"boundaries": [10, 20, 30]}

    def preview(source, path, candidate, settings):
        counts["preview"] += 1
        path.write_bytes(b"preview")
        return {"duration": candidate.end - candidate.start}

    def choose(*args):
        counts["generate"] += 1
        return {
            "clips": [
                SelectedClip(
                    start=1,
                    end=7,
                    candidate_id="candidate-001",
                    title="t",
                    reason="r",
                    confidence=1,
                ).model_dump()
            ],
            "receipt": {"provider": "mock", "generation_requests": 1},
        }

    def render(source, path, clip, settings, ass=None, keep=None, visuals=()):
        counts["render"] += 1
        path.write_bytes(b"complete-render")
        return {
            "duration": clip.end - clip.start,
            "width": 404,
            "height": 720,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    monkeypatch.setattr(media, "require_tools", lambda *a, **kw: None)
    monkeypatch.setattr(
        media,
        "probe",
        lambda *a: {"duration": 40, "width": 320, "height": 180, "audio": True},
    )
    monkeypatch.setattr(media, "preview", preview)
    monkeypatch.setattr(media, "render", render)
    monkeypatch.setattr(signals, "transcribe", asr)
    monkeypatch.setattr(signals, "scene_boundaries", scenes)
    monkeypatch.setattr(selection, "choose", choose)
    return source, output, counts


def test_full_orchestration_and_exact_resume(job, settings):
    source, output, counts = job
    original = digest(source)
    result = pipeline.run(source, output, settings)
    assert result["status"] == "complete"
    assert result["human_review_performed"] is False
    assert len(result["rendered"]) == 1
    before = dict(counts)
    second = pipeline.run(source, output, settings, resume=True)
    # Timings and the source of the reused transcript describe this invocation.
    assert result["transcript_cache"]["source"] == "asr"
    assert second["transcript_cache"]["source"] == "output_stage"
    assert {**second, "timings": None, "transcript_cache": None} == {
        **result,
        "timings": None,
        "transcript_cache": None,
    }
    assert counts == before
    assert digest(source) == original
    assert not list((output / ".analysis").glob("*.mp4"))


def test_analysis_stops_before_previews_models_and_renders(job, settings, tmp_path):
    source, output, counts = job
    result = pipeline.run(source, output, settings, analyze_only=True)
    assert result["status"] == "analyzed"
    assert result["candidates"]
    assert result["selection_receipt"]["generation_requests"] == 0
    assert result["human_review_performed"] is False
    assert counts == {"asr": 1, "scenes": 1, "generate": 0, "render": 0, "preview": 0}
    manual = tmp_path / "selection.json"
    write_json(
        manual,
        {
            "clips": [
                {
                    "start": 1,
                    "end": 7,
                    "candidate_id": "manual",
                    "title": "topic",
                    "reason": "reviewed interval",
                    "confidence": 1,
                }
            ]
        },
    )
    complete = pipeline.run(
        source, output, settings, resume=True, selection_file=manual
    )
    assert complete["status"] == "complete"
    assert counts["asr"] == counts["scenes"] == 1
    assert counts["generate"] == counts["preview"] == 0
    saved = (output / "manifest.json").read_bytes()
    with pytest.raises(PipelineError, match="without rendered clips"):
        pipeline.run(source, output, settings, resume=True, analyze_only=True)
    assert (output / "manifest.json").read_bytes() == saved


def test_required_cache_miss_stops_before_any_analysis(job, settings, tmp_path):
    source, output, counts = job
    settings.transcript_cache = str(tmp_path / "cache")
    settings.require_transcript_cache = True
    with pytest.raises(PipelineError, match="ASR was not started"):
        pipeline.run(source, output, settings, analyze_only=True)
    assert not any(counts.values())


def test_resume_rejects_different_input_or_settings(job, settings):
    source, output, _ = job
    pipeline.run(source, output, settings)
    settings.clips = 1
    with pytest.raises(PipelineError, match="differ"):
        pipeline.run(source, output, settings, resume=True)
    assert json.loads((output / "manifest.json").read_text())["status"] == "complete"


def test_second_writer_refused(job, settings):
    source, output, _ = job
    *_, lock = pipeline.prepare(source, output, False, settings)
    try:
        with pytest.raises(PipelineError, match="another job"):
            pipeline.prepare(source, output, True, settings)
    finally:
        lock.close()


def test_source_inside_output_refused(job, settings):
    source, _, _ = job
    with pytest.raises(PipelineError, match="outside"):
        pipeline.prepare(source, source.parent, True, settings)


def test_symlink_output_refused(job, settings, tmp_path):
    source, output, _ = job
    actual = tmp_path / "actual"
    actual.mkdir()
    output.symlink_to(actual, target_is_directory=True)
    with pytest.raises(PipelineError, match="symlink"):
        pipeline.prepare(source, output, False, settings)


def test_symlink_inside_output_refused(job, settings, tmp_path):
    source, output, _ = job
    output.mkdir()
    (output / "link").symlink_to(source)
    with pytest.raises(PipelineError, match="symlink"):
        pipeline.prepare(source, output, True, settings)


def test_zero_byte_source_rejected_before_probe(job, settings):
    source, output, _ = job
    source.write_bytes(b"")
    with pytest.raises(PipelineError, match="size"):
        pipeline.prepare(source, output, False, settings)


def test_failed_generation_records_failure_and_can_resume(job, settings, monkeypatch):
    source, output, counts = job
    original = selection.choose
    monkeypatch.setattr(
        selection,
        "choose",
        lambda *a: (_ for _ in ()).throw(PipelineError("generation unavailable")),
    )
    with pytest.raises(PipelineError):
        pipeline.run(source, output, settings)
    assert json.loads((output / "failure.json").read_text())["complete"] is False
    assert json.loads((output / "manifest.json").read_text())["status"] == "failed"
    assert counts["render"] == 0
    monkeypatch.setattr(selection, "choose", original)
    pipeline.run(source, output, settings, resume=True)
    assert counts["asr"] == 1 and counts["scenes"] == 1
    assert not (output / "failure.json").exists()


def test_cut_failure_cannot_leave_stale_success(job, settings, monkeypatch):
    source, output, _ = job
    pipeline.cut(source, output, 1, 7, settings)
    (output / "clips/clip-001/video.mp4").write_bytes(b"corrupted")
    with pytest.raises(PipelineError):
        pipeline.cut(source, output, 1, 7, settings, resume=True)
    assert json.loads((output / "manifest.json").read_text())["status"] == "failed"
    assert (output / "failure.json").is_file()


def test_modified_input_during_processing_cannot_complete(job, settings, monkeypatch):
    source, output, _ = job
    original = media.render

    def modify(*a, **kw):
        value = original(*a, **kw)
        source.write_bytes(b"changed input while processing")
        return value

    monkeypatch.setattr(media, "render", modify)
    with pytest.raises(PipelineError, match="input changed"):
        pipeline.run(source, output, settings)
    assert json.loads((output / "manifest.json").read_text())["status"] == "failed"


def test_preview_failure_cleans_earlier_previews(job, settings, monkeypatch):
    source, output, _ = job
    # With one worker the second preview is the first failure, so nothing after it may start.
    settings.workers = 1
    original = media.preview
    calls = []

    def fail_second(*args):
        calls.append(1)
        result = original(*args)
        if len(calls) == 2:
            raise PipelineError("preview decode failure")
        return result

    monkeypatch.setattr(media, "preview", fail_second)
    with pytest.raises(PipelineError):
        pipeline.run(source, output, settings)
    assert len(calls) == 2
    assert not list((output / ".analysis").glob("*.mp4"))


def test_manual_selection_bypasses_gemini(job, settings, tmp_path):
    source, output, counts = job
    manual = tmp_path / "manual.json"
    write_json(
        manual,
        {
            "clips": [
                SelectedClip(
                    start=5,
                    end=12,
                    candidate_id="manual",
                    title="title",
                    reason="human choice",
                    confidence=1,
                ).model_dump()
            ]
        },
    )
    result = pipeline.run(source, output, settings, selection_file=manual)
    assert result["selection_receipt"]["generation_requests"] == 0
    assert counts["generate"] == counts["preview"] == 0


def test_manual_cut_does_not_transcribe_or_call_model(job, settings):
    source, output, counts = job
    result = pipeline.cut(source, output, 2, 5, settings)
    assert result["status"] == "complete"
    assert counts["asr"] == counts["generate"] == counts["preview"] == 0
    assert counts["render"] == 1


def missing_a_glyph(monkeypatch):
    """Makes every clip's caption font report one missing character."""
    original = pipeline.create_captions

    def create(*args, **kwargs):
        return {**original(*args, **kwargs), "missing_glyphs": ["ー"]}

    monkeypatch.setattr(pipeline, "create_captions", create)


def test_report_gate_keeps_the_clip_and_records_its_failure(job, settings, monkeypatch):
    source, output, _ = job
    missing_a_glyph(monkeypatch)
    settings.quality_gate = "report"
    result = pipeline.run(source, output, settings)
    recorded = json.loads((output / "clips/clip-001/quality.json").read_text())
    assert recorded["passed"] is False
    assert recorded["failures"] == result["rendered"][0]["quality"]["failures"]
    assert "quality.json" in result["rendered"][0]["sha256"]
    assert result["status"] == "complete"


def test_strict_gate_refuses_a_failing_clip_before_it_is_committed(
    job, settings, monkeypatch
):
    source, output, _ = job
    missing_a_glyph(monkeypatch)
    settings.quality_gate = "strict"
    with pytest.raises(PipelineError, match="quality gate"):
        pipeline.run(source, output, settings)
    assert json.loads((output / "manifest.json").read_text())["status"] == "failed"
    assert not (output / "clips" / "clip-001").exists()
    assert not list((output / "clips").glob(".clip-*"))
