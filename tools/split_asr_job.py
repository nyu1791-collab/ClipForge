#!/usr/bin/env python3
"""Recognise one to four audio parts on owned Colab GPUs and publish one analysis result."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from clipforge import asr_split, audio_first, github_cli, pipeline
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, fingerprint, write_json

PRIVATE_REPO = "nyu1791-collab/ai-model-workers"
ROOT = Path(__file__).resolve().parents[1]
# The account runs at most two T4 and two L4 at once (measured 2026-10-22). Every part asks
# for any GPU, the two longest trying L4 first, so a refused kind falls back to the next and
# a part that finds none waits until another part's runtime is released.
FAST_POOL = "L4,T4:high-ram,T4:standard"
POOL = "T4:high-ram,T4:standard,L4"
ALLOCATION_WAIT_SECONDS = 2700
# A part whose runtime failed after allocation is recognised once more on a fresh runtime
# (2026-10-24: one of four runtimes failed in its first seconds; the other three finished).
ATTEMPTS = 2
TIMEOUTS = {"start": ALLOCATION_WAIT_SECONDS + 300, "run": 5400, "stop": 600}


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
            timeout=TIMEOUTS[command],
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
            "workflow_run": workflow_run,
            "status": "asr_part_complete",
            "asr_part": child["asr_part"],
        }.items()
    ) or manifest != {
        "status": "asr_part_complete",
        "asr_part": child["asr_part"],
        "audio_sha256": child["audio_sha256"],
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
    """Validate every part before installing the full transcript and allocating topics."""
    if plan.get("key") != fingerprint(
        {key: value for key, value in plan.items() if key != "key"}
    ):
        raise PipelineError("ASR plan changed")
    request = plan["request"]
    settings = Settings.model_validate(plan["settings"])
    count = len(plan["parts"])
    if (
        settings.asr_parts != count
        or len(part_directories) != count
        or digest(source) != request["video_sha256"]
        or asr_split.transcript_key(
            request["video_sha256"], plan["info"], settings, plan["split"]
        )
        != plan["transcript_key"]
        or fingerprint(plan["split"]) != plan["split_key"]
    ):
        raise PipelineError("ASR source or recognition identity changed")
    parts = []
    for index, (child, directory) in enumerate(
        zip(plan["parts"], part_directories, strict=True)
    ):
        part = child["asr_part"]
        if (
            child["request_id"]
            != asr_split.part_id(request["request_id"], index, count, plan["split_key"])
            or child["source_ref"] != request["source_ref"]
            or part["source_sha256"] != request["video_sha256"]
            or part["transcript_key"] != plan["transcript_key"]
            or part["split_key"] != plan["split_key"]
        ):
            raise PipelineError("ASR plan has inconsistent part identities")
        parts.append((part, verify_part(directory, child, workflow_run)))
    transcript = asr_split.merge(parts, plan["info"]["duration"])
    with audio_first.exclusive(output):
        audio = output / "audio.m4a"
        if audio.exists():
            raise PipelineError("analysis output already contains audio")
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
            source="parakeet",
            reused=False,
            parts=count,
            part_requests=[child["request_id"] for child in plan["parts"]],
            split=plan["split"],
            split_key=plan["split_key"],
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


def state_name(index, attempt=0):
    return f"control-{index}" + (f"-retry{attempt}" if attempt else "")


def last_state(state_root, index):
    """The control record of the part's last attempt."""
    for attempt in reversed(range(ATTEMPTS)):
        record = read_record(state_root / state_name(index, attempt) / "state.json")
        if record:
            return record
    return {}


def pools(plan):
    """The Colab pool of each part: the two longest try L4 first."""
    durations = [
        child["asr_part"]["end"] - child["asr_part"]["start"] for child in plan["parts"]
    ]
    longest = sorted(range(len(durations)), key=lambda i: durations[i], reverse=True)
    return [FAST_POOL if i in longest[:2] else POOL for i in range(len(durations))]


def part_summary(plan, state_root, part_directories):
    """Per-part shape, held time and stage timings, then the compute units of all at once.

    All runtimes draw from one account balance, so the total is the balance before the
    first allocation minus the balance after the last release, and only when nothing else
    of the account was still running. Colab may lag a moment, so it is a lower bound.
    """
    lines, before, after = [], [], []
    for index, directory in enumerate(part_directories):
        state = last_state(state_root, index)
        receipt = read_record(directory / "gpu-receipt.json")
        timings = (
            receipt.get("timings") if isinstance(receipt.get("timings"), dict) else {}
        )
        part = plan["parts"][index]["asr_part"]
        line = (
            f"ASR part {index + 1}: {part['end'] - part['start']:.0f} s "
            f"of audio on {state.get('accelerator', '?')} {state.get('shape', '?')}"
        )
        if all(
            isinstance(state.get(key), (int, float))
            for key in ("allocated_at", "released_at")
        ):
            line += f", held {state['released_at'] - state['allocated_at']:.0f} s"
        stages = [
            f"{name} {timings[name]:.0f} s"
            for name in ("input", "prefetch_wait", "recognise")
            if isinstance(timings.get(name), (int, float))
        ]
        if stages:
            line += " (" + ", ".join(stages) + ")"
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
        raise PipelineError("ASR requires the designated private worker")
    request = json.loads((args.directory / "request.json").read_text())
    if (
        request.get("request_id") != os.environ.get("REQUEST_ID")
        or request.get("source_ref") != os.environ.get("CLIPFORGE_SOURCE_REF")
        or not request.get("audio_source_url")
    ):
        raise PipelineError("ASR parent request identity mismatch")
    settings = Settings.model_validate(request["settings"])
    source = args.directory / "input-video.bin"
    info = audio_first.audio_info(source)
    args.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    plan = asr_split.prepare(source, args.state_root / "input", request, settings, info)
    staged = []
    count = len(plan["parts"])
    part_directories = [args.state_root / f"result-{index}" for index in range(count)]
    part_pools = pools(plan)
    try:

        def stage(index):
            # The private inputs are independent; create and upload them side by side.
            tag = "clipforge-input-" + plan["parts"][index]["request_id"]
            # The workflow deletes every listed tag even if this job is cancelled.
            with (args.state_root / "staged-tags").open("a") as listing:
                listing.write(tag + "\n")
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
                str(directory / "audio.wav"),
                str(directory / "request.json"),
                timeout=600,
            )

        with ThreadPoolExecutor(max_workers=count) as executor:
            list(executor.map(stage, range(count)))

        def recognise(index):
            child = plan["parts"][index]
            environment = {
                **os.environ,
                "REQUEST_ID": child["request_id"],
                "INPUT_TAG": "clipforge-input-" + child["request_id"],
                "CLIPFORGE_SHARED_BALANCE": "1",
                "CLIPFORGE_COLAB_POOL": part_pools[index],
                "CLIPFORGE_ALLOCATION_WAIT_SECONDS": str(ALLOCATION_WAIT_SECONDS),
            }
            output = part_directories[index]
            for attempt in range(ATTEMPTS):
                state = args.state_root / state_name(index, attempt)
                if attempt:
                    print(
                        f"ASR part {index + 1}: retrying on a new runtime", flush=True
                    )
                    shutil.rmtree(output, ignore_errors=True)
                try:
                    # No GPU within the allocation wait is final; waiting again would
                    # only double it.
                    control(args.control_python, state, "start", output, environment)
                    try:
                        control(args.control_python, state, "run", output, environment)
                        return
                    except PipelineError:
                        if attempt + 1 == ATTEMPTS:
                            raise
                finally:
                    # A release that is not confirmed stops the part, so no retry can
                    # hold a second runtime for it.
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
            # The summary only reports; it never decides whether recognition succeeded.
            print("ASR part summary unavailable: " + type(error).__name__, flush=True)
        if errors:
            raise PipelineError(
                "ASR failed; private part outputs were retained"
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
        pipeline.record_failure(args.output, error, "asr")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ASR failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
