#!/usr/bin/env python3
"""Coordinate two to four owned Colab recognisers and publish one ordinary analysis result."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from clipforge import asr_split, audio_first, github_cli, pipeline
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, fingerprint, write_json

PRIVATE_REPO = "nyu1791-collab/ai-model-workers"
ROOT = Path(__file__).resolve().parents[1]


def control(python, state, command, output, environment):
    try:
        result = subprocess.run(
            [
                str(python),
                str(ROOT / "tools/colab_actions.py"),
                command,
                "--state-dir",
                str(state),
                "--output",
                str(output),
            ],
            env=environment,
            timeout={"start": 1200, "run": 5400, "stop": 600}[command],
        )
    except subprocess.TimeoutExpired:
        raise PipelineError("ASR part control timed out during " + command) from None
    if result.returncode:
        raise PipelineError("ASR part control failed during " + command)


def verify_part(directory, child, workflow_run):
    receipt = json.loads((directory / "request-receipt.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    if any(
        receipt.get(key) != value
        for key, value in {
            "request_id": child["request_id"],
            "source_ref": child["source_ref"],
            "audio_source_url": child["audio_source_url"],
            "workflow_run": workflow_run,
            "status": "asr_part_complete",
            "asr_part": child["asr_part"],
        }.items()
    ) or manifest != {
        "status": "asr_part_complete",
        "asr_part": child["asr_part"],
        "source_sha256": child["video_sha256"],
        "rendered": [],
    }:
        raise PipelineError("returned ASR part belongs to another request")
    hashes = receipt.get("sha256")
    required = {"manifest.json", "transcript.json"}
    if (
        not isinstance(hashes, dict)
        # The Colab worker also lists its runtime diagnostics; nothing else is accepted.
        or not required <= set(hashes) <= required | {"gpu-receipt.json"}
        or any(
            (directory / name).is_symlink() or digest(directory / name) != checksum
            for name, checksum in hashes.items()
        )
    ):
        raise PipelineError("returned ASR part checksum mismatch")
    return json.loads((directory / "transcript.json").read_text())


def finish(source, output, plan, part_directories, workflow_run):
    """Validate both independent results before installing a reusable full transcript."""
    if plan.get("key") != fingerprint(
        {key: value for key, value in plan.items() if key != "key"}
    ):
        raise PipelineError("ASR split plan changed")
    request = plan["request"]
    settings = Settings.model_validate(plan["settings"])
    split = plan["split"]
    count = len(plan["parts"])
    if (
        not asr_split.MIN_PARTS <= count <= asr_split.MAX_PARTS
        or settings.asr_parts != count
        or len(part_directories) != count
        or digest(source) != request["video_sha256"]
        or pipeline.transcript_identity(
            source, plan["info"], settings, split=asr_split.split_identity(split)
        )
        != plan["transcript_key"]
    ):
        raise PipelineError("ASR split source or recognition identity changed")
    parts = []
    for index, (child, directory) in enumerate(
        zip(plan["parts"], part_directories, strict=True)
    ):
        if (
            child["request_id"]
            != asr_split.part_id(
                request["request_id"], index, count, asr_split.split_key(split)
            )
            or child["source_ref"] != request["source_ref"]
            or child["audio_source_url"] != request["audio_source_url"]
            or child["settings"]
            != settings.model_copy(update={"asr_parts": 1}).model_dump()
            or child["asr_part"]["source_sha256"] != request["video_sha256"]
            or child["asr_part"]["transcript_key"] != plan["transcript_key"]
            or child["asr_part"]["split_key"] != asr_split.split_key(split)
        ):
            raise PipelineError("ASR split plan has inconsistent part identities")
        parts.append((child["asr_part"], verify_part(directory, child, workflow_run)))
    transcript = asr_split.merge(parts, plan["info"]["duration"])
    with audio_first.exclusive(output):
        audio = output / "audio.m4a"
        if audio.exists():
            raise PipelineError("split analysis output already contains audio")
        shutil.copyfile(source, audio)
        key = plan["transcript_key"]
        write_json(output / "transcripts" / (key + ".json"), transcript)
        readonly = settings.model_copy(
            update={"require_transcript_cache": True, "transcript_key": key}
        )
        analysis, _ = audio_first.allocate_audio(
            audio, output, readonly, request["audio_source_url"]
        )
        cache = json.loads((output / "asr-receipt.json").read_text())
        cache.update(
            source="split_asr",
            reused=False,
            parts=len(plan["parts"]),
            overlap_seconds=asr_split.OVERLAP_SECONDS,
            part_requests=[child["request_id"] for child in plan["parts"]],
            split=split,
            split_key=asr_split.split_key(split),
        )
        write_json(output / "asr-receipt.json", cache)
        write_json(
            output / "manifest.json",
            {**analysis, "source_sha256": request["video_sha256"], "rendered": []},
        )
        names = [
            "audio.m4a",
            "manifest.json",
            "topics.json",
            "asr-receipt.json",
            "transcripts/" + key + ".json",
        ]
        write_json(
            output / "request-receipt.json",
            {
                "request_id": request["request_id"],
                "source_ref": request["source_ref"],
                "workflow_run": workflow_run,
                "status": "topics_allocated",
                "rendered_clips": 0,
                "audio_source_url": request["audio_source_url"],
                "sha256": {name: digest(output / name) for name in names},
            },
        )


def read_record(path):
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


# Four parts run on two L4 and two T4 runtimes, the two L4 going to the two longest parts.
# The account's concurrent limits are two T4 and two L4 (measured 2026-10-22).
MIXED_POOLS = {"L4": "L4", "T4": "T4:high-ram,T4:standard"}


def accelerator_pool(plan, index):
    """The Colab pool for one part; None keeps the default pool for the chosen shape."""
    count = len(plan["parts"])
    if count != 4:
        return None
    durations = [child["asr_part"]["duration"] for child in plan["parts"]]
    longest = sorted(range(count), key=lambda item: durations[item], reverse=True)[:2]
    return MIXED_POOLS["L4" if index in longest else "T4"]


def part_summary(plan, state_root, part_directories):
    """Per-part shape, held time and stage timings, then the compute units of both at once.

    Both runtimes draw from one account balance, so the total is the balance before the
    first allocation minus the balance after the last release, and only when nothing else
    of the account was still running. Colab may lag a moment, so it is a lower bound.
    """
    lines, before, after = [], [], []
    for index, directory in enumerate(part_directories):
        state = read_record(state_root / f"control-{index}" / "state.json")
        receipt = read_record(directory / "gpu-receipt.json")
        timings = (
            receipt.get("timings") if isinstance(receipt.get("timings"), dict) else {}
        )
        line = (
            f"ASR part {index + 1}: {plan['parts'][index]['asr_part']['duration']:.0f} s "
            f"of audio on {state.get('accelerator', '?')} {state.get('shape', '?')}"
        )
        if all(
            isinstance(state.get(key), (int, float))
            for key in ("allocated_at", "released_at")
        ):
            line += f", held {state['released_at'] - state['allocated_at']:.0f} s"
        stages = [
            f"{name} {timings[name]:.0f} s"
            for name in ("setup", "prepare", "pipeline")
            if isinstance(timings.get(name), (int, float))
        ]
        if stages:
            line += " (" + ", ".join(stages) + ")"
        host = receipt.get("host")
        if isinstance(host, dict) and isinstance(host.get("cpu_count"), int):
            line += f", {host['cpu_count']} cores"
        lines.append(line)
        if isinstance(state.get("units_before"), dict):
            before.append(state["units_before"])
        if isinstance(state.get("units_after"), dict):
            after.append((state.get("released_at") or 0, state["units_after"]))
    count = len(part_directories)
    if len(before) == count and len(after) == count:
        last = max(after, key=lambda item: item[0])[1]
        if last.get("assignments") == 0:
            first = max(units["balance"] for units in before)
            lines.append(
                f"Compute units used by all ASR parts: {first - last['balance']:.2f}"
            )
            return lines
    lines.append("Compute units used by all ASR parts: not determinable")
    return lines


def run(args):
    if (
        os.environ.get("GITHUB_REPOSITORY") != PRIVATE_REPO
        or github_cli.run("api", "repos/" + PRIVATE_REPO, "--jq", ".private").strip()
        != "true"
    ):
        raise PipelineError("split ASR requires the designated private worker")
    request = json.loads((args.directory / "request.json").read_text())
    if (
        request.get("request_id") != os.environ.get("REQUEST_ID")
        or request.get("source_ref") != os.environ.get("CLIPFORGE_SOURCE_REF")
        or not request.get("audio_source_url")
    ):
        raise PipelineError("split ASR parent request identity mismatch")
    settings = Settings.model_validate(request["settings"]).model_copy(
        update={"whisper_device": "cuda", "whisper_compute_type": "float16"}
    )
    source = args.directory / "input-video.bin"
    info = audio_first.audio_info(source)
    plan = asr_split.prepare(source, args.state_root / "input", request, settings, info)
    staged = []
    count = len(plan["parts"])
    part_directories = [args.state_root / f"result-{index}" for index in range(count)]
    try:

        def stage(index):
            # The private inputs are independent; create and upload them side by side.
            tag = "clipforge-input-" + plan["parts"][index]["request_id"]
            github_cli.run(
                "release",
                "create",
                tag,
                "--repo",
                PRIVATE_REPO,
                "--target",
                "main",
                "--title",
                tag,
                "--latest=false",
                "--notes",
                "Temporary private ClipForge ASR part.",
            )
            staged.append(tag)
            directory = args.state_root / "input" / f"part-{index}"
            github_cli.run(
                "release",
                "upload",
                tag,
                "--repo",
                PRIVATE_REPO,
                str(directory / "input-video.bin"),
                str(directory / "request.json"),
                timeout=600,
            )

        with ThreadPoolExecutor(max_workers=count) as executor:
            list(executor.map(stage, range(count)))

        # The longer part allocates first. Colab has granted only one T4 high-memory
        # runtime at a time, so the faster setup goes to the part with more audio and
        # the other part falls back to the next shape in the pool.
        leader = max(
            range(count), key=lambda index: plan["parts"][index]["asr_part"]["duration"]
        )
        leader_allocated = threading.Event()
        leader_failed = threading.Event()

        def recognise(index):
            child = plan["parts"][index]
            environment = {
                **os.environ,
                "REQUEST_ID": child["request_id"],
                "INPUT_TAG": "clipforge-input-" + child["request_id"],
                "CLIPFORGE_SHARED_BALANCE": "1",
            }
            pool = accelerator_pool(plan, index)
            if pool is None:
                environment.pop("CLIPFORGE_COLAB_POOL", None)
            else:
                environment["CLIPFORGE_COLAB_POOL"] = pool
            state, output = (
                args.state_root / f"control-{index}",
                part_directories[index],
            )
            if index != leader:
                leader_allocated.wait()
                if leader_failed.is_set():
                    raise PipelineError("the other ASR part could not start")
            try:
                try:
                    control(args.control_python, state, "start", output, environment)
                except BaseException:
                    if index == leader:
                        leader_failed.set()
                    raise
                finally:
                    if index == leader:
                        leader_allocated.set()
                control(args.control_python, state, "run", output, environment)
            finally:
                if (state / "state.json").exists():
                    control(args.control_python, state, "stop", output, environment)

        with ThreadPoolExecutor(max_workers=count) as executor:
            futures = [executor.submit(recognise, index) for index in range(count)]
            errors = []
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    errors.append(error)
        try:
            for line in part_summary(plan, args.state_root, part_directories):
                print(line, flush=True)
        except Exception as error:
            # The summary only reports; it never decides whether the split succeeded.
            print("ASR part summary unavailable: " + type(error).__name__, flush=True)
        if errors:
            raise PipelineError(
                "split ASR failed; private part outputs were retained"
            ) from None
        finish(source, args.output, plan, part_directories, os.environ["GITHUB_RUN_ID"])
        print("All ASR parts verified and merged; topics allocated once.", flush=True)
    finally:
        for tag in staged:
            try:
                github_cli.run(
                    "release",
                    "delete",
                    tag,
                    "--repo",
                    PRIVATE_REPO,
                    "--yes",
                    "--cleanup-tag",
                )
            except RuntimeError:
                print(
                    "Temporary ASR input cleanup remains for the workflow cleanup step.",
                    file=sys.stderr,
                )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--control-python", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args)
    except Exception as error:
        args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        pipeline.record_failure(args.output, error, "split_asr")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Split ASR failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
