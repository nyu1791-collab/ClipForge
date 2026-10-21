import json


from clipforge import pipeline
from clipforge.models import Settings
from clipforge.storage import PipelineError


def make(**overrides):
    return Settings(
        **{
            "gemini_model": "manual-no-api",
            "min_seconds": 3,
            "max_seconds": 30,
            **overrides,
        }
    )


def test_one_failing_video_does_not_stop_the_others(monkeypatch, tmp_path):
    def fake_run(source, output, settings, resume=False, *args):
        if "broken" in str(source):
            raise PipelineError("input is not a video")
        return {"status": "complete", "rendered": [{}, {}]}

    monkeypatch.setattr(pipeline, "run", fake_run)
    inputs = [tmp_path / "first.mp4", tmp_path / "broken.mp4", tmp_path / "third.mp4"]
    results = pipeline.run_batch(inputs, tmp_path / "out", make(), concurrency=2)
    assert [item["status"] for item in results] == ["complete", "failed", "complete"]
    assert results[1]["error"] == "input is not a video"
    assert results[0]["rendered"] == 2
    saved = json.loads((tmp_path / "out" / "batch.json").read_text())
    assert saved["results"] == results
    assert saved["human_review_performed"] is False


def test_folder_names_are_unique_and_safe(tmp_path):
    assert pipeline._folder_name(tmp_path / "動画 (1)?.mp4") == "動画__1"
    assert pipeline._folder_name(tmp_path / "a/b.mp4") == "b"
    assert pipeline._folder_name(tmp_path / ".mp4") == "mp4"
    assert pipeline._folder_name(tmp_path / "...mp4") == "video"


def test_default_concurrency_leaves_cores_for_each_video(monkeypatch):
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 4)
    assert pipeline.default_concurrency(make(threads=2, workers=2)) == 1
    assert pipeline.default_concurrency(make(threads=1, workers=1)) == 4
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 1)
    assert pipeline.default_concurrency(make(threads=8, workers=4)) == 1
