import argparse
import importlib
from pathlib import Path


def load(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("dispatch_parallel_render")


def arguments(clips=5):
    return argparse.Namespace(
        editorial_release="clipforge-kick-editorial-1", previous_run=1,
        previous_request="a" * 32, config="config.landscape-gpu-plain.example.json",
        source_ref="b" * 40, source_url="https://example.test/video", clips=clips,
    )


def test_each_clip_gets_its_own_gpu_render_run(monkeypatch):
    m = load(monkeypatch)
    parts = m.plan(arguments())
    assert [p["clip_index"] for p in parts] == [1, 2, 3, 4, 5]
    assert len({p["request_id"] for p in parts}) == 5
    for part in parts:
        inputs = part["inputs"]
        assert inputs["clip_index"] == str(part["clip_index"])
        assert inputs["audio_stage"] == "render"
        assert inputs["worker"] == "colab-auto"
        assert inputs["auto_render"] is False
        assert inputs["continue_missing"] is False
        assert inputs["previous_request_id"] == "a" * 32


def test_merge_run_takes_run_request_pairs_in_the_merge_format(monkeypatch):
    m = load(monkeypatch)
    part_runs = ",".join(f"{100 + i}:{i + 1:032x}" for i in range(5))
    assert m.PART_RUNS.fullmatch(part_runs)
    inputs = m.merge_inputs("c" * 32, part_runs, arguments())
    assert inputs["audio_stage"] == "merge"
    assert inputs["part_runs"] == part_runs
    assert inputs["previous_request_id"] == "a" * 32
