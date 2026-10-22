import json

import pytest

from clipforge import hardware, pipeline
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


@pytest.mark.parametrize(
    ("cores", "videos", "concurrency", "explicit", "expected"),
    [
        (8, 4, 4, 0, 2),
        (9, 4, 4, 0, 2),
        (2, 4, 4, 0, 1),
        (8, 2, 4, 0, 4),
        (8, 4, 1, 0, 0),
        (8, 1, 4, 0, 0),
        (8, 4, 0, 0, 0),
        (8, 4, 4, 6, 6),
    ],
)
def test_batch_shares_only_automatic_render_threads(
    monkeypatch, tmp_path, cores, videos, concurrency, explicit, expected
):
    monkeypatch.setattr(hardware.os, "sched_getaffinity", lambda pid: set(range(cores)))
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 64)
    settings = make(threads=2, encode_threads=explicit)
    original = settings.model_dump()
    received = []

    def fake_run(source, output, effective_settings, resume=False):
        received.append(effective_settings)
        assert resume is True
        return {"status": "complete", "rendered": []}

    monkeypatch.setattr(pipeline, "run", fake_run)
    inputs = [tmp_path / f"synthetic-{index}.mp4" for index in range(videos)]
    results = pipeline.run_batch(
        inputs, tmp_path / "out", settings, concurrency, resume=True
    )
    assert len(received) == len(results) == videos
    assert all(result["status"] == "complete" for result in results)
    for effective in received:
        assert effective.model_dump() == {**original, "encode_threads": expected}
        if expected == explicit:
            assert effective is settings
        else:
            assert effective is not settings
    assert settings.model_dump() == original


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
