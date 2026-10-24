import copy
import json
import sys
from types import SimpleNamespace
import threading
import wave

import faster_whisper
import pytest

from clipforge import asr_split, audio_first, pipeline
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json
from tools import run_request, split_asr_job, submit_job

URL = "https://cdn.example/audio.mp3?signature=private-test"


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "audio.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * 16000 * 40)
    settings = Settings(
        asr_parts=2,
        whisper_device="cuda",
        whisper_compute_type="float16",
        min_seconds=3,
        max_seconds=10,
    )
    request = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "video_sha256": digest(source),
        "video_bytes": source.stat().st_size,
        "settings": settings.model_dump(),
        "audio_source_url": URL,
        "chat_sha256": None,
    }
    plan = asr_split.prepare(
        source, tmp_path / "parts", request, settings, audio_first.audio_info(source)
    )
    return source, plan, tmp_path


def transcript(words):
    return {
        "language": "ja",
        "segments": [
            {
                "start": start,
                "end": end,
                "text": text,
                "words": [{"start": start, "end": end, "text": text}],
            }
            for start, end, text in words
        ],
    }


def results(plan):
    return [
        (
            plan["parts"][0]["asr_part"],
            transcript([(1, 2, "前半"), (19.7, 20.3, "境界")]),
        ),
        (
            plan["parts"][1]["asr_part"],
            transcript([(4.7, 5.3, "境界"), (20, 21, "後半")]),
        ),
    ]


def test_lossless_split_has_overlap_and_preserves_parent_identity(prepared):
    source, plan, root = prepared
    assert plan["boundaries"][0] == 20
    assert [child["asr_part"]["offset"] for child in plan["parts"]] == [0, 15]
    assert [child["asr_part"]["duration"] for child in plan["parts"]] == [25, 25]
    assert not (root / "parts/audio.wav").exists()
    for index, child in enumerate(plan["parts"]):
        audio = root / f"parts/part-{index}/input-video.bin"
        assert digest(audio) == child["video_sha256"]
        assert child["request_id"] != plan["request"]["request_id"]
        assert child["asr_part"]["source_sha256"] == digest(source)
        assert child["settings"]["asr_parts"] == 1
        assert audio_first.audio_info(audio)["duration"] == 25


def test_merge_owns_boundary_words_once_and_restores_full_clock(prepared):
    _, plan, _ = prepared
    value = asr_split.merge(list(reversed(results(plan))), 40)
    assert [row["text"] for row in value["segments"]] == ["前半", "境界", "後半"]
    assert [row["start"] for row in value["segments"]] == [1, 19.7, 35]


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "foreign", "gap", "duration", "bad_time"]
)
def test_merge_refuses_incomplete_or_unrelated_parts(prepared, change):
    _, plan, _ = prepared
    parts = copy.deepcopy(results(plan))
    if change == "missing":
        parts.pop()
    elif change == "duplicate":
        parts[1] = parts[0]
    elif change == "foreign":
        parts[1][0]["source_sha256"] = "c" * 64
    elif change == "gap":
        parts[1][0]["core_start"] += 1
    elif change == "duration":
        parts[1][0]["duration"] = 1
    else:
        parts[1][0]["offset"] = float("nan")
    with pytest.raises(PipelineError):
        asr_split.merge(parts, 40)


def test_ambiguous_boundary_fails_without_clipping_words(prepared):
    _, plan, _ = prepared
    parts = results(plan)
    parts[0][1]["segments"] = transcript([(19.6, 20.2, "左")])["segments"]
    parts[1][1]["segments"] = transcript([(4.8, 5.4, "右")])["segments"]
    with pytest.raises(PipelineError, match="ambiguous"):
        asr_split.merge(parts, 40)


def merged_words(plan, front, back):
    value = asr_split.merge(
        [
            (plan["parts"][0]["asr_part"], front),
            (plan["parts"][1]["asr_part"], back),
        ],
        40,
    )
    return [
        (word["text"], round(word["start"], 6))
        for segment in value["segments"]
        for word in segment["words"]
    ]


def test_word_straddling_the_cut_is_kept_once_when_each_side_places_it_differently(
    prepared,
):
    _, plan, _ = prepared
    # The front half hears the word after the cut (midpoint 20.1), the back half before it (19.9).
    front = transcript([(1, 2, "前半"), (19.8, 20.4, "境界")])
    back = transcript([(4.6, 5.2, "境界"), (20, 21, "後半")])
    assert merged_words(plan, front, back) == [
        ("前半", 1),
        ("境界", 19.8),
        ("後半", 35),
    ]


@pytest.mark.parametrize("side", ["front", "back"])
def test_word_heard_by_one_side_near_the_cut_is_kept(prepared, side):
    _, plan, _ = prepared
    front = transcript(
        [(1, 2, "前半")] + ([(19.8, 20.4, "境界")] if side == "front" else [])
    )
    back = transcript(
        ([(4.6, 5.2, "境界")] if side == "back" else []) + [(20, 21, "後半")]
    )
    start = 19.8 if side == "front" else 19.6
    assert merged_words(plan, front, back) == [
        ("前半", 1),
        ("境界", start),
        ("後半", 35),
    ]


def test_word_beyond_the_rescue_distance_is_still_dropped(prepared):
    _, plan, _ = prepared
    # Midpoint 21.5 is 1.5 s past the cut, so it is not a candidate and is discarded.
    front = transcript([(1, 2, "前半"), (21, 22, "遠い")])
    back = transcript([(4.7, 5.3, "境界"), (20, 21, "後半")])
    assert merged_words(plan, front, back) == [
        ("前半", 1),
        ("境界", 19.7),
        ("後半", 35),
    ]


def test_rescued_word_joins_its_own_segment_in_time_order(prepared):
    _, plan, _ = prepared
    front = transcript([(1, 2, "前半")])
    back = {
        "language": "ja",
        "segments": [
            {
                "start": 4.6,
                "end": 6.0,
                "text": "境界後半",
                "words": [
                    {"start": 4.6, "end": 5.2, "text": "境界"},
                    {"start": 5.5, "end": 6.0, "text": "後半"},
                ],
            }
        ],
    }
    value = asr_split.merge(
        [
            (plan["parts"][0]["asr_part"], front),
            (plan["parts"][1]["asr_part"], back),
        ],
        40,
    )
    assert len(value["segments"]) == 2
    joined = value["segments"][1]
    assert joined["text"] == "境界後半"
    assert [word["text"] for word in joined["words"]] == ["境界", "後半"]
    assert round(joined["start"], 6) == 19.6 and round(joined["end"], 6) == 21


def test_overlapping_rescued_words_from_both_sides_keep_the_front_half(prepared):
    _, plan, _ = prepared
    # Front: 左 (midpoint 20.2) and back: 右 (midpoint 19.8) overlap, so only the front one stays.
    front = transcript([(1, 2, "前半"), (19.9, 20.5, "左")])
    back = transcript([(4.5, 5.1, "右"), (20, 21, "後半")])
    assert merged_words(plan, front, back) == [
        ("前半", 1),
        ("左", 19.9),
        ("後半", 35),
    ]


def test_full_split_identity_is_separate_and_cannot_silently_run_sequentially(prepared):
    source, plan, _ = prepared
    settings = Settings.model_validate(plan["settings"])
    assert (
        pipeline.transcript_identity(
            source, plan["info"], settings.model_copy(update={"asr_parts": 1})
        )
        != plan["transcript_key"]
    )
    with pytest.raises(PipelineError, match="coordinator"):
        pipeline.transcribe_once(source, plan["info"], settings)


def test_joined_analysis_can_be_collected_and_reused_without_asr(prepared, monkeypatch):
    source, plan, root = prepared
    directories = []
    for index, (descriptor, raw) in enumerate(results(plan)):
        child = plan["parts"][index]
        directory = root / f"result-{index}"
        directories.append(directory)
        write_json(directory / "transcript.json", raw)
        write_json(
            directory / "manifest.json",
            {
                "status": "asr_part_complete",
                "asr_part": descriptor,
                "source_sha256": child["video_sha256"],
                "rendered": [],
            },
        )
        names = ["manifest.json", "transcript.json"]
        if index == 0:
            # The Colab worker adds its runtime diagnostics to the checksum list.
            write_json(directory / "gpu-receipt.json", {"timings": {}})
            names.append("gpu-receipt.json")
        write_json(
            directory / "request-receipt.json",
            {
                "request_id": child["request_id"],
                "source_ref": child["source_ref"],
                "audio_source_url": URL,
                "workflow_run": "123",
                "status": "asr_part_complete",
                "asr_part": descriptor,
                "sha256": {name: digest(directory / name) for name in names},
            },
        )
    monkeypatch.setattr(
        pipeline.signals,
        "transcribe",
        lambda *a, **k: pytest.fail("merge must never recognise again"),
    )
    output = root / "analysis"
    split_asr_job.finish(source, output, plan, directories, "123")
    submit_job.verify_audio_analysis(
        output, "a" * 32, "b" * 40, digest(source), URL, "123"
    )
    assert json.loads((output / "asr-receipt.json").read_text())["parts"] == 2
    receipt = json.loads((output / "asr-receipt.json").read_text())
    assert receipt["split"] == plan["split"]
    assert receipt["split_key"] == asr_split.split_key(plan["split"])
    (directories[1] / "transcript.json").write_text("{}")
    with pytest.raises(PipelineError, match="checksum"):
        split_asr_job.finish(source, root / "tampered", plan, directories, "123")


def test_gpu_worker_recognises_only_its_part_and_writes_verified_receipts(
    prepared, monkeypatch
):
    _, plan, root = prepared
    child = plan["parts"][1]
    raw = results(plan)[1][1]
    calls = []

    def recognise(source, info, settings, journal=None):
        calls.append((info["duration"], settings.whisper_device, settings.asr_parts))
        return raw

    monkeypatch.setattr(pipeline.signals, "transcribe", recognise)
    monkeypatch.setenv("GITHUB_REPOSITORY", split_asr_job.PRIVATE_REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", child["source_ref"])
    monkeypatch.setenv("CLIPFORGE_REQUIRE_CUDA", "1")
    output = root / "worker-result"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_request.py",
            "run",
            "--tag",
            "clipforge-input-" + child["request_id"],
            "--directory",
            str(root / "parts/part-1"),
            "--output",
            str(output),
        ],
    )
    run_request.main()
    assert calls == [(25, "cuda", 1)]
    assert split_asr_job.verify_part(output, child, "123") == raw
    assert not (output / "topics.json").exists()


@pytest.mark.parametrize("failed_index", [None, 0])
def test_coordinator_runs_both_parts_concurrently_and_stops_each_owned_runtime(
    prepared, monkeypatch, failed_index
):
    source, plan, root = prepared
    directory = root / "parent"
    directory.mkdir()
    (directory / "input-video.bin").write_bytes(source.read_bytes())
    write_json(directory / "request.json", plan["request"])
    monkeypatch.setattr(asr_split, "prepare", lambda *a: plan)
    monkeypatch.setenv("GITHUB_REPOSITORY", split_asr_job.PRIVATE_REPO)
    monkeypatch.setenv("REQUEST_ID", plan["request"]["request_id"])
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", plan["request"]["source_ref"])
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    operations, stopped, started, active = [], [], [], set()
    barrier = threading.Barrier(2)

    def gh(*arguments, **kwargs):
        operations.append(arguments)
        return "true" if arguments[0] == "api" else ""

    monkeypatch.setattr(split_asr_job.github_cli, "run", gh)

    def control(python, state, command, output, environment):
        index = int(state.name[-1])
        child = plan["parts"][index]
        assert environment["REQUEST_ID"] == child["request_id"]
        assert "GEMINI_API_KEY" not in environment
        assert environment["CLIPFORGE_SHARED_BALANCE"] == "1"
        if command == "start":
            # Allocation is ordered: the longer part asks first for the faster shape.
            started.append(index)
            write_json(state / "state.json", {"status": "allocated"})
        elif command == "stop":
            stopped.append(index)
        else:
            # Recognition itself still runs on both runtimes at once.
            active.add(index)
            barrier.wait(timeout=5)
            assert active == {0, 1}
            if index == failed_index:
                raise PipelineError("synthetic part failure")
            write_json(
                output / "manifest.json",
                {
                    "status": "asr_part_complete",
                    "asr_part": child["asr_part"],
                    "source_sha256": child["video_sha256"],
                    "rendered": [],
                },
            )
            write_json(output / "transcript.json", results(plan)[index][1])
            write_json(
                output / "request-receipt.json",
                {
                    "request_id": child["request_id"],
                    "source_ref": child["source_ref"],
                    "audio_source_url": URL,
                    "workflow_run": "123",
                    "status": "asr_part_complete",
                    "asr_part": child["asr_part"],
                    "sha256": {
                        name: digest(output / name)
                        for name in ("manifest.json", "transcript.json")
                    },
                },
            )

    monkeypatch.setattr(split_asr_job, "control", control)
    args = SimpleNamespace(
        directory=directory,
        output=root / "combined",
        control_python="python",
        state_root=root / "coordinator",
    )
    if failed_index is None:
        split_asr_job.run(args)
        submit_job.verify_audio_analysis(
            args.output, "a" * 32, "b" * 40, digest(source), URL, "123"
        )
    else:
        with pytest.raises(PipelineError, match="split ASR failed"):
            split_asr_job.run(args)
        assert not (args.output / "topics.json").exists()
    longer = max(
        range(2), key=lambda index: plan["parts"][index]["asr_part"]["duration"]
    )
    assert started == [longer, 1 - longer]
    assert sorted(stopped) == [0, 1]
    assert len([row for row in operations if row[:2] == ("release", "delete")]) == 2


def test_failed_leader_allocation_keeps_the_other_part_from_allocating(
    prepared, monkeypatch
):
    source, plan, root = prepared
    directory = root / "parent"
    directory.mkdir()
    (directory / "input-video.bin").write_bytes(source.read_bytes())
    write_json(directory / "request.json", plan["request"])
    monkeypatch.setattr(asr_split, "prepare", lambda *a: plan)
    monkeypatch.setenv("GITHUB_REPOSITORY", split_asr_job.PRIVATE_REPO)
    monkeypatch.setenv("REQUEST_ID", plan["request"]["request_id"])
    monkeypatch.setenv("CLIPFORGE_SOURCE_REF", plan["request"]["source_ref"])
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setattr(
        split_asr_job.github_cli,
        "run",
        lambda *arguments, **kwargs: "true" if arguments[0] == "api" else "",
    )
    calls = []

    def control(python, state, command, output, environment):
        calls.append((int(state.name[-1]), command))
        raise PipelineError("synthetic refusal")

    monkeypatch.setattr(split_asr_job, "control", control)
    args = SimpleNamespace(
        directory=directory,
        output=root / "combined",
        control_python="python",
        state_root=root / "coordinator",
    )
    with pytest.raises(PipelineError, match="split ASR failed"):
        split_asr_job.run(args)
    longer = max(
        range(2), key=lambda index: plan["parts"][index]["asr_part"]["duration"]
    )
    assert calls == [(longer, "start")]


def test_part_summary_reports_shapes_timings_and_one_total(prepared, tmp_path):
    _, plan, _ = prepared
    root = tmp_path / "coordinator"
    directories = [tmp_path / "result-0", tmp_path / "result-1"]
    for index, (shape, held, after) in enumerate(
        [
            ("HIGH_RAM", (100.0, 437.0), (183.93, 1)),
            ("STANDARD", (103.0, 460.0), (183.93, 0)),
        ]
    ):
        write_json(
            root / f"control-{index}" / "state.json",
            {
                "accelerator": "T4",
                "shape": shape,
                "allocated_at": held[0],
                "released_at": held[1],
                "units_before": {
                    "balance": 184.11,
                    "hourly_rate": 0.0,
                    "assignments": 0,
                },
                "units_after": {
                    "balance": after[0],
                    "hourly_rate": 0.0,
                    "assignments": after[1],
                },
            },
        )
        write_json(
            directories[index] / "gpu-receipt.json",
            {
                "timings": {"setup": 33.5, "prepare": 5.1, "pipeline": 290.2},
                "host": {"cpu_count": 8, "memory_gib": 51.0},
            },
        )
    lines = split_asr_job.part_summary(plan, root, directories)
    assert lines[0].startswith("ASR part 1: ")
    assert (
        "on T4 HIGH_RAM, held 337 s (setup 34 s, prepare 5 s, pipeline 290 s), 8 cores"
        in lines[0]
    )
    assert "on T4 STANDARD, held 357 s" in lines[1]
    assert lines[2] == "Compute units used by all ASR parts: 0.18"
    # A runtime still active at the last release makes the account balance ambiguous.
    state = json.loads((root / "control-1" / "state.json").read_text())
    state["units_after"]["assignments"] = 1
    write_json(root / "control-1" / "state.json", state)
    assert split_asr_job.part_summary(plan, root, directories)[2].endswith(
        "not determinable"
    )
    # Missing receipts never break the summary.
    assert len(split_asr_job.part_summary(plan, tmp_path / "missing", directories)) == 3


@pytest.mark.parametrize(
    "explicit,configured,expected",
    [(None, None, 4), (1, None, 1), (None, 1, 1), (2, 1, 2)],
)
def test_url_submission_defaults_to_four_parts_and_respects_explicit_choice(
    tmp_path, monkeypatch, explicit, configured, expected
):
    def gh(*arguments, **kwargs):
        if arguments[0] == "run":
            return "[]"
        if arguments[0] == "secret":
            return '[{"name":"COLAB_ADC_JSON"}]'
        return (
            '{"state":"active"}'
            if "workflows" in arguments[1]
            else '{"sha":"' + "b" * 40 + '"}'
        )

    monkeypatch.setattr(submit_job, "gh", gh)
    monkeypatch.setattr(
        submit_job, "preflight_audio_url", lambda *a: {"has_video": False}
    )
    monkeypatch.setattr(submit_job, "send_dispatch", lambda *a: None)
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps({} if configured is None else {"asr_parts": configured})
    )
    args = SimpleNamespace(
        worker="colab-auto",
        audio_analysis_url=URL,
        input=None,
        config=config,
        clips=None,
        chat_file=None,
        chat_url=None,
        ref="b" * 40,
        auto_previews=False,
        asr_parts=explicit,
        public_cache="off",
    )
    state, _ = submit_job.dispatch(args, tmp_path)
    assert (
        json.loads((state / "request.json").read_text())["settings"]["asr_parts"]
        == expected
    )


@pytest.mark.parametrize("value", [True, False, 0, 5, 2.0, "2"])
def test_asr_parts_requires_an_exact_supported_integer(value):
    with pytest.raises(ValueError, match="asr_parts"):
        Settings(asr_parts=value)


SPEECH_RATE = 16000


def cut(tmp_path, name, search, speech=None, origin=None):
    """Plan a 40 s split with a fake VAD over absolute speech spans (seconds), if given."""
    source = tmp_path / f"{name}.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, SPEECH_RATE, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * SPEECH_RATE * 40)
    settings = Settings(
        asr_parts=2,
        whisper_device="cuda",
        whisper_compute_type="float16",
        min_seconds=3,
        max_seconds=10,
        asr_split_search_seconds=search,
    )
    request = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "video_sha256": digest(source),
        "video_bytes": source.stat().st_size,
        "settings": settings.model_dump(),
        "audio_source_url": URL,
        "chat_sha256": None,
    }
    return asr_split.prepare(
        source,
        tmp_path / f"parts-{name}",
        request,
        settings,
        audio_first.audio_info(source),
    )


def fake_vad(monkeypatch, speech, origin):
    """Stand-in for Silero: reports the speech spans that fall inside the audio it is given."""
    seen = []

    def get_speech_timestamps(audio, vad_options=None, sampling_rate=16000, **kwargs):
        seen.append(len(audio))
        end = origin + len(audio) / sampling_rate
        return [
            {
                "start": round((max(start, origin) - origin) * sampling_rate),
                "end": round((min(stop, end) - origin) * sampling_rate),
            }
            for start, stop in speech
            if min(stop, end) > max(start, origin)
        ]

    monkeypatch.setattr(
        faster_whisper.vad, "get_speech_timestamps", get_speech_timestamps
    )
    return seen


def test_split_cuts_at_longest_silence_inside_the_search_window(tmp_path, monkeypatch):
    # Window is 10 s to 30 s (the quarter limits bind at 1800 s), so origin is 10 s.
    seen = fake_vad(
        monkeypatch,
        [(10.5, 11.0), (11.2, 14.0), (16.0, 17.0), (18.0, 19.0)],
        origin=10,
    )
    plan = cut(tmp_path, "longest", 1800)
    split = plan["split"]["cuts"][0]
    assert (split["method"], split["reason"]) == ("vad", "longest_silence")
    assert split["silence"] == [14.0, 16.0]
    assert plan["boundaries"][0] == 15.0
    assert seen == [20 * SPEECH_RATE]
    assert split["elapsed_seconds"] >= 0


def test_split_scales_pcm_in_place_to_the_same_float_samples(tmp_path, monkeypatch):
    import numpy as np

    source = tmp_path / "pcm.wav"
    values = np.random.default_rng(7).integers(
        -32768, 32768, size=40 * SPEECH_RATE, dtype="<i2"
    )
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, SPEECH_RATE, 0, "NONE", "not compressed"))
        handle.writeframes(values.tobytes())
    captured = []

    def get_speech_timestamps(audio, vad_options=None, sampling_rate=16000, **kwargs):
        captured.append(audio.copy())
        return []

    monkeypatch.setattr(
        faster_whisper.vad, "get_speech_timestamps", get_speech_timestamps
    )
    with wave.open(str(source), "rb") as handle:
        asr_split.choose_cuts(handle, SPEECH_RATE, handle.getnframes(), 0, 1800, 2)
    # Window is 10 s to 30 s, the same as the scaled PCM read from the file.
    expected = values[10 * SPEECH_RATE : 30 * SPEECH_RATE].astype(np.float32) / 32768.0
    assert captured[0].dtype == np.float32
    assert np.array_equal(captured[0], expected)
    assert captured[0].min() == -1.0


def test_split_ignores_long_silence_outside_the_search_window(tmp_path, monkeypatch):
    # With 3 s of search, the window is 17 s to 23 s; the 8 s silence before it is out of reach.
    fake_vad(monkeypatch, [(5, 6), (14, 14.5), (17, 17.5), (18.5, 19)], origin=17)
    plan = cut(tmp_path, "outside", 3)
    assert plan["split"]["cuts"][0]["window"] == [17.0, 23.0]
    assert plan["split"]["cuts"][0]["silence"] == [17.5, 18.5]
    assert plan["boundaries"][0] == 18.0


def test_equal_silences_prefer_the_one_nearest_the_middle(tmp_path, monkeypatch):
    # Both gaps are 2 s; the one at 22.5-24.5 s (centre 23.5) is nearer the middle at 20 s.
    fake_vad(monkeypatch, [(11, 15), (17, 22.5), (24.5, 25)], origin=10)
    plan = cut(tmp_path, "nearest", 1800)
    assert plan["split"]["cuts"][0]["silence"] == [22.5, 24.5]
    assert plan["boundaries"][0] == 23.5


def test_search_window_is_kept_inside_the_middle_half(tmp_path, monkeypatch):
    # A 3600 s search would reach past the ends; the window stops at a quarter of the audio.
    seen = fake_vad(monkeypatch, [(11, 12), (13, 14)], origin=10)
    plan = cut(tmp_path, "quarter", 3600)
    assert plan["split"]["cuts"][0]["window"] == [10.0, 30.0]
    assert seen == [20 * SPEECH_RATE]
    assert 10 <= plan["boundaries"][0] <= 30
    assert plan["boundaries"][0] == 12.5


def test_no_silence_falls_back_to_the_middle_with_a_reason(tmp_path, monkeypatch):
    fake_vad(monkeypatch, [(10, 30)], origin=10)
    plan = cut(tmp_path, "continuous", 1800)
    assert (plan["split"]["cuts"][0]["method"], plan["split"]["cuts"][0]["reason"]) == (
        "middle",
        "no_silence",
    )
    assert plan["split"]["cuts"][0]["silence"] is None
    assert plan["boundaries"][0] == 20


@pytest.mark.parametrize("module", ["faster_whisper.vad", "onnxruntime"])
def test_missing_vad_falls_back_to_the_middle_with_a_reason(
    tmp_path, monkeypatch, module
):
    monkeypatch.delattr(faster_whisper, "vad", raising=False)
    monkeypatch.setitem(sys.modules, module, None)
    plan = cut(tmp_path, "novad", 1800)
    assert (plan["split"]["cuts"][0]["method"], plan["split"]["cuts"][0]["reason"]) == (
        "middle",
        "vad_unavailable",
    )
    assert plan["boundaries"][0] == 20


def test_failing_vad_falls_back_to_the_middle_with_a_reason(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("model load failed")

    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", broken)
    plan = cut(tmp_path, "broken", 1800)
    assert (plan["split"]["cuts"][0]["method"], plan["split"]["cuts"][0]["reason"]) == (
        "middle",
        "vad_failed",
    )
    assert plan["boundaries"][0] == 20


def test_zero_search_never_runs_vad(tmp_path, monkeypatch):
    seen = fake_vad(monkeypatch, [(11, 12), (13, 14)], origin=10)
    plan = cut(tmp_path, "zero", 0)
    assert (plan["split"]["cuts"][0]["method"], plan["split"]["cuts"][0]["reason"]) == (
        "middle",
        "search_disabled",
    )
    assert seen == []
    assert plan["boundaries"][0] == 20


def test_cut_identity_separates_caches_and_part_ids_but_not_timings(prepared):
    source, plan, _ = prepared
    settings = Settings.model_validate(plan["settings"])
    base = plan["split"]
    cut = base["cuts"][0]

    def with_cut(**changes):
        return {**base, "cuts": [{**cut, **changes}]}

    moved = with_cut(sample=cut["sample"] + SPEECH_RATE, method="vad")
    variants = [
        base,
        moved,
        {**base, "search_seconds": 600},
        with_cut(silence=[1.0, 2.0]),
    ]
    keys = [
        pipeline.transcript_identity(
            source, plan["info"], settings, split=asr_split.split_identity(split)
        )
        for split in variants
    ]
    assert len(set(keys)) == len(variants)
    assert keys[0] == plan["transcript_key"]
    assert pipeline.transcript_identity(source, plan["info"], settings) not in keys
    timed = {**base, "cuts": [{**cut, "elapsed_seconds": 99.0, "reason": "other"}]}
    assert asr_split.split_key(timed) == asr_split.split_key(base)
    parent = "a" * 32
    assert asr_split.part_id(
        parent, 0, 2, asr_split.split_key(base)
    ) != asr_split.part_id(parent, 0, 2, asr_split.split_key(moved))
    assert plan["parts"][0]["asr_part"]["split_key"] == asr_split.split_key(base)


@pytest.mark.parametrize("count", [3, 4])
def test_more_parts_cover_the_audio_once_and_merge_each_word_once(tmp_path, count):
    source = tmp_path / "audio.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * 16000 * 40)
    settings = Settings(
        asr_parts=count,
        asr_split_search_seconds=0,
        whisper_device="cuda",
        whisper_compute_type="float16",
        min_seconds=3,
        max_seconds=10,
    )
    request = {
        "request_id": "a" * 32,
        "source_ref": "b" * 40,
        "video_sha256": digest(source),
        "video_bytes": source.stat().st_size,
        "settings": settings.model_dump(),
        "audio_source_url": URL,
        "chat_sha256": None,
    }
    plan = asr_split.prepare(
        source,
        tmp_path / "parts",
        request,
        settings,
        audio_first.audio_info(source),
    )
    assert len(plan["parts"]) == count
    assert len(plan["boundaries"]) == count - 1
    assert len({child["request_id"] for child in plan["parts"]}) == count
    truth = [(t, t + 0.5, f"w{t}") for t in range(1, 40, 2)]
    parts = []
    for child in plan["parts"]:
        descriptor = child["asr_part"]
        start, stop = (
            descriptor["offset"],
            descriptor["offset"] + descriptor["duration"],
        )
        words = [
            (s - start, e - start, text)
            for s, e, text in truth
            if s >= start and e <= stop
        ]
        parts.append((descriptor, transcript(words)))
    merged = asr_split.merge(parts, plan["info"]["duration"])
    got = [word["text"] for segment in merged["segments"] for word in segment["words"]]
    assert got == [text for _, _, text in truth]
