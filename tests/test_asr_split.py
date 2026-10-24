import json
from types import SimpleNamespace
import threading
import wave

import numpy as np
import pytest

from clipforge import asr, asr_split, audio_first, media
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json
from tools import split_asr_job, submit_job

URL = "https://cdn.example/audio.mp3?signature=private-test"
RATE = asr.SAMPLE_RATE


def write_wav(path, samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        handle.writeframes(pcm.tobytes())


def speech(seconds, quiet=()):
    """A loud tone with silent spans (start, end) in seconds."""
    samples = 0.5 * np.sin(np.arange(int(seconds * RATE)) / 3.0).astype(np.float32)
    for start, end in quiet:
        samples[int(start * RATE) : int(end * RATE)] = 0
    return samples


def prepare(tmp_path, monkeypatch, samples, parts):
    """Plan an ASR request whose decoded audio is exactly the given samples."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"synthetic source")
    monkeypatch.setattr(
        media,
        "audio",
        lambda source, destination, duration: write_wav(destination, samples),
    )
    settings = Settings(asr_parts=parts, language="ja", min_seconds=3, max_seconds=10)
    request = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "video_sha256": digest(source),
        "video_bytes": source.stat().st_size,
        "settings": settings.model_dump(),
        "audio_source_url": URL,
    }
    info = {"audio": True, "duration": len(samples) / RATE, "audio_start": 0.0}
    plan = asr_split.prepare(source, tmp_path / "parts", request, settings, info)
    return source, plan


def part_transcript(rows):
    return {
        "language": "ja",
        "segments": [
            {
                "start": start,
                "end": end,
                "text": text,
                "words": [{"start": start, "end": end, "text": text}],
            }
            for start, end, text in rows
        ],
    }


def test_cuts_fall_in_the_quietest_moment_near_each_even_share(tmp_path, monkeypatch):
    samples = speech(400, quiet=[(95, 96), (210, 211), (290, 291)])
    _, plan = prepare(tmp_path, monkeypatch, samples, 4)
    cuts = [cut / RATE for cut in plan["split"]["cuts"]]
    spans = [(95, 96), (210, 211), (290, 291)]
    assert all(lo < cut < hi for cut, (lo, hi) in zip(cuts, spans, strict=True))


def test_parts_cover_the_audio_exactly_once_with_their_own_checksums(
    tmp_path, monkeypatch
):
    samples = speech(120, quiet=[(59, 61)])
    source, plan = prepare(tmp_path, monkeypatch, samples, 2)
    joined = b""
    for index, child in enumerate(plan["parts"]):
        directory = tmp_path / "parts" / f"part-{index}"
        audio = directory / "audio.wav"
        assert child["audio_sha256"] == digest(audio)
        assert child["audio_bytes"] == audio.stat().st_size
        assert json.loads((directory / "request.json").read_text()) == child
        with wave.open(str(audio), "rb") as handle:
            joined += handle.readframes(handle.getnframes())
    expected = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    assert joined == expected
    first, second = (child["asr_part"] for child in plan["parts"])
    assert first["start"] == 0 and first["end"] == second["start"]
    assert second["end"] == pytest.approx(120)
    assert first["source_sha256"] == plan["request"]["video_sha256"] == digest(source)
    assert not (tmp_path / "parts" / "audio.wav").exists()


def test_one_part_is_the_whole_audio(tmp_path, monkeypatch):
    _, plan = prepare(tmp_path, monkeypatch, speech(30), 1)
    (child,) = plan["parts"]
    assert plan["split"]["cuts"] == []
    assert child["asr_part"]["start"] == 0
    assert child["asr_part"]["end"] == pytest.approx(30)


def test_part_ids_and_transcript_key_follow_the_cuts(tmp_path, monkeypatch):
    _, quiet_early = prepare(tmp_path / "a", monkeypatch, speech(120, [(50, 51)]), 2)
    _, quiet_late = prepare(tmp_path / "b", monkeypatch, speech(120, [(70, 71)]), 2)
    assert quiet_early["split_key"] != quiet_late["split_key"]
    assert quiet_early["transcript_key"] != quiet_late["transcript_key"]
    assert quiet_early["parts"][0]["request_id"] != quiet_late["parts"][0]["request_id"]
    _, again = prepare(tmp_path / "c", monkeypatch, speech(120, [(50, 51)]), 2)
    assert again["transcript_key"] == quiet_early["transcript_key"]
    assert again["parts"][0]["request_id"] == quiet_early["parts"][0]["request_id"]


@pytest.mark.parametrize(
    "arguments",
    [
        ("A" * 32, 0, 2, "c" * 64),
        ("a" * 32, 2, 2, "c" * 64),
        ("a" * 32, 0, 5, "c" * 64),
        ("a" * 32, 0, 2, "short"),
        ("a" * 32, True, 2, "c" * 64),
    ],
)
def test_part_id_rejects_invalid_identities(arguments):
    with pytest.raises(PipelineError):
        asr_split.part_id(*arguments)


def test_merge_shifts_each_part_to_the_source_clock(tmp_path, monkeypatch):
    _, plan = prepare(tmp_path, monkeypatch, speech(120, [(59, 61)]), 2)
    first, second = (child["asr_part"] for child in plan["parts"])
    merged = asr_split.merge(
        [
            (second, part_transcript([(1, 2, "後半")])),
            (first, part_transcript([(1, 2, "前半")])),
        ],
        120,
    )
    assert [(s["start"], s["text"]) for s in merged["segments"]] == [
        (1, "前半"),
        (pytest.approx(second["start"] + 1), "後半"),
    ]
    assert merged["segments"][1]["words"][0]["start"] == pytest.approx(
        second["start"] + 1
    )


@pytest.mark.parametrize("fault", ["missing", "gap", "parent", "beyond", "language"])
def test_merge_refuses_parts_that_do_not_cover_the_same_source(
    tmp_path, monkeypatch, fault
):
    _, plan = prepare(tmp_path, monkeypatch, speech(120, [(59, 61)]), 2)
    first, second = (dict(child["asr_part"]) for child in plan["parts"])
    rows = [
        (first, part_transcript([(1, 2, "前半")])),
        (second, part_transcript([(1, 2, "後半")])),
    ]
    if fault == "missing":
        rows.pop()
    elif fault == "gap":
        second["start"] += 1
    elif fault == "parent":
        second["parent_request_id"] = "c" * 32
    elif fault == "beyond":
        rows[0] = (first, part_transcript([(1, first["end"] + 5, "長すぎ")]))
    else:
        rows[1][1]["language"] = "en"
    with pytest.raises(PipelineError):
        asr_split.merge(rows, 120)


def test_asr_parts_defaults_to_four_and_requires_an_exact_integer():
    assert Settings().asr_parts == 4
    for value in (True, 0, 5, 2.0, "2"):
        with pytest.raises(ValueError):
            Settings(asr_parts=value)


def results(plan):
    return [
        (child, part_transcript([(1, 2, f"話題{index}")]))
        for index, child in enumerate(plan["parts"])
    ]


def write_part_result(directory, child, raw, diagnostics=False):
    write_json(directory / "transcript.json", raw)
    write_json(
        directory / "manifest.json",
        {
            "status": "asr_part_complete",
            "asr_part": child["asr_part"],
            "audio_sha256": child["audio_sha256"],
        },
    )
    names = ["manifest.json", "transcript.json"]
    if diagnostics:
        # The Colab worker adds its runtime diagnostics to the checksum list.
        write_json(directory / "gpu-receipt.json", {"timings": {}})
        names.append("gpu-receipt.json")
    write_json(
        directory / "request-receipt.json",
        {
            "request_id": child["request_id"],
            "source_ref": child["source_ref"],
            "workflow_run": "123",
            "status": "asr_part_complete",
            "rendered_clips": 0,
            "asr_part": child["asr_part"],
            "sha256": {name: digest(directory / name) for name in names},
        },
    )


def test_joined_analysis_can_be_collected_and_tampering_is_refused(
    tmp_path, monkeypatch
):
    source, plan = prepare(tmp_path, monkeypatch, speech(120, [(59, 61)]), 2)
    monkeypatch.setattr(
        audio_first,
        "audio_info",
        lambda path: {"audio": True, "duration": 120.0, "audio_start": 0.0},
    )
    directories = []
    for index, (child, raw) in enumerate(results(plan)):
        directory = tmp_path / f"result-{index}"
        directories.append(directory)
        write_part_result(directory, child, raw, diagnostics=index == 0)
    output = tmp_path / "analysis"
    split_asr_job.finish(source, output, plan, directories, "123")
    submit_job.verify_audio_analysis(
        output, "a" * 32, "b" * 40, digest(source), URL, "123"
    )
    receipt = json.loads((output / "asr-receipt.json").read_text())
    assert receipt["parts"] == 2 and receipt["source"] == "parakeet"
    assert receipt["split"] == plan["split"]
    (directories[1] / "transcript.json").write_text("{}")
    with pytest.raises(PipelineError, match="checksum"):
        split_asr_job.finish(source, tmp_path / "tampered", plan, directories, "123")


def coordinator(tmp_path, monkeypatch, source, plan):
    directory = tmp_path / "parent"
    directory.mkdir()
    (directory / "input-video.bin").write_bytes(source.read_bytes())
    write_json(directory / "request.json", plan["request"])
    monkeypatch.setattr(asr_split, "prepare", lambda *a: plan)
    monkeypatch.setattr(
        audio_first,
        "audio_info",
        lambda path: {"audio": True, "duration": 400.0, "audio_start": 0.0},
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", split_asr_job.PRIVATE_REPO)
    monkeypatch.setenv("REQUEST_ID", plan["request"]["request_id"])
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", plan["request"]["source_ref"])
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    operations = []

    def gh(*arguments, **kwargs):
        operations.append(arguments)
        return "true" if arguments[0] == "api" else ""

    monkeypatch.setattr(split_asr_job.github_cli, "run", gh)
    args = SimpleNamespace(
        directory=directory,
        output=tmp_path / "combined",
        control_python="python",
        state_root=tmp_path / "coordinator",
    )
    return args, operations


@pytest.mark.parametrize("failed_index", [None, 2])
def test_four_parts_run_side_by_side_on_any_gpu_and_stop_every_runtime(
    tmp_path, monkeypatch, failed_index
):
    source, plan = prepare(
        tmp_path, monkeypatch, speech(400, [(95, 96), (210, 211), (290, 291)]), 4
    )
    args, operations = coordinator(tmp_path, monkeypatch, source, plan)
    stopped, active = [], set()
    barrier = threading.Barrier(4)
    pools = {}

    def control(python, state, command, output, environment):
        index = int(state.name[-1])
        child = plan["parts"][index]
        assert environment["REQUEST_ID"] == child["request_id"]
        assert environment["CLIPFORGE_SHARED_BALANCE"] == "1"
        assert environment["CLIPFORGE_ALLOCATION_WAIT_SECONDS"] == "2700"
        if command == "start":
            pools[index] = environment["CLIPFORGE_COLAB_POOL"]
            write_json(state / "state.json", {"status": "allocated"})
        elif command == "stop":
            stopped.append(index)
        else:
            # All four parts are recognised at once.
            active.add(index)
            barrier.wait(timeout=5)
            assert active == {0, 1, 2, 3}
            if index == failed_index:
                raise PipelineError("synthetic part failure")
            write_part_result(output, child, results(plan)[index][1])

    monkeypatch.setattr(split_asr_job, "control", control)
    if failed_index is None:
        split_asr_job.run(args)
        submit_job.verify_audio_analysis(
            args.output, "a" * 32, "b" * 40, digest(source), URL, "123"
        )
    else:
        with pytest.raises(PipelineError, match="ASR failed"):
            split_asr_job.run(args)
        assert not (args.output / "topics.json").exists()
    durations = [c["asr_part"]["end"] - c["asr_part"]["start"] for c in plan["parts"]]
    longest = sorted(range(4), key=lambda i: durations[i], reverse=True)[:2]
    assert {i for i, pool in pools.items() if pool.startswith("L4")} == set(longest)
    assert all("L4" in pool and "T4" in pool for pool in pools.values())
    assert sorted(stopped) == [0, 1, 2, 3]
    deleted = [row[2] for row in operations if row[:2] == ("release", "delete")]
    staged = (args.state_root / "staged-tags").read_text().split()
    assert sorted(deleted) == sorted(staged) and len(staged) == 4


def test_a_refused_part_still_stops_its_own_state_and_fails_the_job(
    tmp_path, monkeypatch
):
    source, plan = prepare(tmp_path, monkeypatch, speech(120, [(59, 61)]), 2)
    args, _ = coordinator(tmp_path, monkeypatch, source, plan)
    calls = []

    def control(python, state, command, output, environment):
        calls.append((int(state.name[-1]), command))
        raise PipelineError("synthetic refusal")

    monkeypatch.setattr(split_asr_job, "control", control)
    with pytest.raises(PipelineError, match="ASR failed"):
        split_asr_job.run(args)
    assert sorted(calls) == [(0, "start"), (1, "start")]


def test_part_summary_reports_shapes_timings_and_one_total(tmp_path, monkeypatch):
    _, plan = prepare(tmp_path, monkeypatch, speech(120, [(59, 61)]), 2)
    root = tmp_path / "coordinator"
    directories = [tmp_path / "result-0", tmp_path / "result-1"]
    for index, (accelerator, held, after) in enumerate(
        [("L4", (100.0, 300.0), (183.93, 1)), ("T4", (103.0, 320.0), (183.90, 0))]
    ):
        write_json(
            root / f"control-{index}" / "state.json",
            {
                "accelerator": accelerator,
                "shape": "HIGH_RAM",
                "allocated_at": held[0],
                "released_at": held[1],
                "units_before": {"balance": 184.0, "assignments": index},
                "units_after": {"balance": after[0], "assignments": after[1]},
            },
        )
        write_json(
            directories[index] / "gpu-receipt.json",
            {"timings": {"input": 3.0, "prefetch_wait": 40.0, "recognise": 60.0}},
        )
    lines = split_asr_job.part_summary(plan, root, directories)
    assert lines[0].startswith("ASR part 1: ")
    assert (
        "on L4 HIGH_RAM, held 200 s (input 3 s, prefetch_wait 40 s, recognise 60 s)"
        in lines[0]
    )
    assert lines[-1] == "Compute units used by all ASR parts: 0.10"


def test_pieces_are_cut_at_the_quietest_moment_near_each_boundary():
    samples = speech(1100, quiet=[(470, 471), (960, 961)])
    cuts = [cut / RATE for cut in asr.cut_points(samples)]
    assert cuts[0] == 0 and cuts[-1] == pytest.approx(1100)
    assert 470 < cuts[1] < 471 and 960 < cuts[2] < 961


def test_short_audio_is_one_piece():
    assert asr.cut_points(np.zeros(RATE * 100, dtype=np.float32)) == [0, RATE * 100]


def test_character_runs_are_cut_at_pauses_and_shifted_to_the_source():
    chars = [
        {"char": "あ", "start": 0.0, "end": 0.1},
        {"char": "い", "start": 0.1, "end": 0.2},
        {"char": " ", "start": 0.2, "end": 0.2},
        {"char": "う", "start": 1.0, "end": 1.1},
    ]
    rows = asr.segments(chars, 10.0, 20.0)
    assert [(row["start"], row["text"]) for row in rows] == [
        (10.0, "あい"),
        (11.0, "う"),
    ]
    assert all(w["end"] > w["start"] for row in rows for w in row["words"])


def test_overlong_runs_are_split_and_characters_past_the_end_dropped():
    chars = [{"char": "字", "start": i * 0.1, "end": i * 0.1 + 0.09} for i in range(40)]
    chars[20]["start"] += 0.2
    rows = asr.segments(chars, 0.0, 3.0)
    assert all(len(row["text"]) <= asr.LONGEST_CHARS for row in rows)
    assert all(row["end"] <= 3.0 for row in rows)


def test_recognise_runs_every_piece_once_and_writes_source_seconds(
    tmp_path, monkeypatch
):
    audio = tmp_path / "part.wav"
    write_wav(audio, speech(600, quiet=[(480, 481)]))
    seen = []

    class Result:
        def __init__(self):
            self.timestamp = {"char": [{"char": "語", "start": 1.0, "end": 1.2}]}

    class Model:
        def cuda(self):
            return self

        def eval(self):
            return self

        def transcribe(self, paths, timestamps, batch_size):
            seen.append((len(paths), timestamps, batch_size))
            return [Result() for _ in paths]

    monkeypatch.setattr(asr, "prepare", lambda load=True: Model())
    output = tmp_path / "transcript.json"
    asr.recognise(audio, output, "ja")
    value = json.loads(output.read_text())
    assert seen == [(2, True, 1)]
    assert value["language"] == "ja"
    starts = [row["start"] for row in value["segments"]]
    assert starts[0] == 1.0 and 481 < starts[1] < 482.5


@pytest.mark.parametrize("failing", ["run", "start", "stop"])
def test_a_failed_runtime_is_retried_once_only_after_a_confirmed_release(
    tmp_path, monkeypatch, failing
):
    source, plan = prepare(tmp_path, monkeypatch, speech(120, [(59, 61)]), 2)
    args, _ = coordinator(tmp_path, monkeypatch, source, plan)
    calls = []

    def control(python, state, command, output, environment):
        index = int(state.name.split("-")[1])
        calls.append((index, state.name, command))
        if command == "start":
            write_json(state / "state.json", {"status": "allocated"})
        first = state.name == "control-0"
        if first and command == failing:
            raise PipelineError("synthetic first-attempt failure")
        if command == "run":
            write_part_result(output, plan["parts"][index], results(plan)[index][1])

    monkeypatch.setattr(split_asr_job, "control", control)
    if failing == "run":
        split_asr_job.run(args)
        assert [c for c in calls if c[0] == 0] == [
            (0, "control-0", "start"),
            (0, "control-0", "run"),
            (0, "control-0", "stop"),
            (0, "control-0-retry1", "start"),
            (0, "control-0-retry1", "run"),
            (0, "control-0-retry1", "stop"),
        ]
        assert (args.output / "topics.json").exists()
    else:
        with pytest.raises(PipelineError, match="ASR failed"):
            split_asr_job.run(args)
        assert not any(name.endswith("retry1") for _, name, _ in calls)
