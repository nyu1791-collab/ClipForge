#!/usr/bin/env python3
"""Recognise only selected topic audio with the existing private Colab ASR worker."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor

from colab_actions import Controller, ControlError
from colab_bundle import ReleaseBundles, digest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clipforge.storage import write_json

REPOSITORY = "nyu1791-collab/ai-model-workers"
RATE = 16000


def github(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ControlError(
            "Selected-audio ASR transfer failed; no automatic resubmission"
        )
    return result.stdout


def prepare_audio(source, clips, work):
    """One bounded GPU input containing chosen audio only, separated by silence."""
    audio = work / "audio.wav"
    mappings, frames = [], 0
    with wave.open(str(audio), "wb") as joined:
        joined.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        for index, clip in enumerate(clips):
            part = work / f"topic-{index}.wav"
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    str(clip["start"]),
                    "-i",
                    str(source),
                    "-t",
                    str(clip["end"] - clip["start"]),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(RATE),
                    "-c:a",
                    "pcm_s16le",
                    str(part),
                ],
                capture_output=True,
                timeout=1200,
            )
            if result.returncode:
                raise ControlError("Selected topic audio could not be decoded")
            with wave.open(str(part), "rb") as handle:
                if handle.getnframes() == 0:
                    raise ControlError("Selected topic has no audio")
                start = frames / RATE
                while data := handle.readframes(RATE * 2):
                    joined.writeframesraw(data)
                    frames += len(data) // 2
                mappings.append({"start": start, "end": frames / RATE})
            part.unlink()
            if index + 1 < len(clips):
                joined.writeframesraw(b"\0\0" * (RATE // 2))
                frames += RATE // 2
    return audio, mappings


def split_transcript(value, mappings, language):
    """Return GPU text and word timestamps to each topic's own clock."""
    clips = []
    for mapping in mappings:
        begin, finish = mapping["start"], mapping["end"]
        rows = []
        for row in value.get("segments", []):
            words = [
                {
                    **word,
                    "start": round(max(0, word["start"] - begin), 3),
                    "end": round(min(finish, word["end"]) - begin, 3),
                }
                for word in row.get("words", [])
                if begin <= word["start"] < finish and word["end"] > word["start"]
            ]
            if words:
                rows.append(
                    {
                        "start": words[0]["start"],
                        "end": words[-1]["end"],
                        "text": "".join(word["text"] for word in words),
                        "words": words,
                    }
                )
            elif not row.get("words") and begin <= row["start"] < finish:
                rows.append(
                    {
                        **row,
                        "start": round(row["start"] - begin, 3),
                        "end": round(min(row["end"], finish) - begin, 3),
                    }
                )
        clips.append({"language": language, "segments": rows})
    return clips


def run_parallel(source, plan_file, output):
    """Run disjoint selected topics on up to four separately owned GPU runtimes."""
    plan = json.loads(plan_file.read_text())
    clips = plan["clips"]
    count = min(4, len(clips))
    if count <= 1:
        return run(source, plan_file, output)
    state_path = output / "selected-gpu-asr-state.json"
    if state_path.exists():
        raise ControlError(
            "Selected GPU ASR was already attempted; inspect its saved state"
        )
    groups = [[] for _ in range(count)]
    loads = [0.0] * count
    for index in sorted(
        range(len(clips)),
        key=lambda i: clips[i]["end"] - clips[i]["start"],
        reverse=True,
    ):
        slot = min(range(count), key=loads.__getitem__)
        groups[slot].append(index)
        loads[slot] += clips[index]["end"] - clips[index]["start"]
    root = output / "selected-gpu-parts"
    root.mkdir(mode=0o700)
    state = {
        "input_key": plan["input_key"],
        "status": "preparing",
        "started_at": time.time(),
        "parts": [],
    }
    for slot, indices in enumerate(groups):
        indices.sort()
        part = root / str(slot)
        part.mkdir(mode=0o700)
        part_key = hashlib.sha256(
            (plan["input_key"] + ":part:" + str(slot)).encode()
        ).hexdigest()
        part_plan = {
            **plan,
            "input_key": part_key,
            "clips": [clips[i] for i in indices],
        }
        write_json(part / "plan.json", part_plan)
        state["parts"].append(
            {"directory": str(slot), "indices": indices, "input_key": part_key}
        )
    state["status"] = "gpu_requests_or_unknown"
    write_json(state_path, state)

    def run_part(slot):
        part = root / str(slot)
        env = {**os.environ, "CLIPFORGE_SHARED_BALANCE": "1"}
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--single",
                "--source",
                str(source),
                "--plan",
                str(part / "plan.json"),
                "--output",
                str(part),
            ],
            env=env,
        )
        if result.returncode:
            raise ControlError(
                "A selected-audio GPU part failed; saved part states were retained"
            )

    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(run_part, slot) for slot in range(count)]
            errors = []
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    errors.append(error)
        if errors:
            raise ControlError(
                "Selected-audio GPU parts did not all complete; inspect saved states"
            )
        ordered = [None] * len(clips)
        for part_info in state["parts"]:
            part_result = json.loads(
                (root / part_info["directory"] / "selected-gpu-asr.json").read_text()
            )
            if (
                part_result.get("status") != "complete"
                or part_result.get("input_key") != part_info["input_key"]
                or len(part_result["clips"]) != len(part_info["indices"])
            ):
                raise ControlError("Selected-audio part identity or clip count differs")
            for index, transcript in zip(part_info["indices"], part_result["clips"]):
                ordered[index] = transcript
        if any(item is None for item in ordered):
            raise ControlError("A selected-audio transcript is missing")
        write_json(
            output / "selected-gpu-asr.json",
            {
                "status": "complete",
                "input_key": plan["input_key"],
                "recognizer": plan["recognizer"],
                "timestamp_source": "gpu_asr",
                "gpu_parts": count,
                "clips": ordered,
            },
        )
        state["status"] = "complete"
    finally:
        for part_info in state["parts"]:
            part_state = root / part_info["directory"] / "selected-gpu-asr-state.json"
            if part_state.is_file():
                part_info["state"] = json.loads(part_state.read_text())
        state["elapsed_sec"] = round(time.time() - state["started_at"], 3)
        write_json(state_path, state)


def run(source, plan_file, output):
    plan = json.loads(plan_file.read_text())
    state_path = output / "selected-gpu-asr-state.json"
    if state_path.exists():
        raise ControlError(
            "Selected GPU ASR was already attempted; inspect its saved state"
        )
    source_ref, parent = (
        os.environ.get("CLIPFORGE_SOURCE_REF", ""),
        os.environ.get("REQUEST_ID", ""),
    )
    if not re.fullmatch(r"[a-f0-9]{40}", source_ref) or not re.fullmatch(
        r"[a-f0-9]{32}", parent
    ):
        raise ControlError("Selected GPU ASR requires the saved production identity")
    child = hashlib.sha256(
        (parent + ":selected-gpu-asr:" + plan["input_key"]).encode()
    ).hexdigest()[:32]
    tag = "clipforge-input-" + child
    state = {
        "input_key": plan["input_key"],
        "request_id": child,
        "input_tag": tag,
        "source_ref": source_ref,
        "status": "preparing",
        "started_at": time.time(),
    }
    write_json(state_path, state)
    work = output / "selected-gpu-work"
    work.mkdir(mode=0o700)
    controller = Controller(work / "control")
    try:
        audio, mappings = prepare_audio(source, plan["clips"], work)
        request = {
            "request_id": child,
            "source_ref": source_ref,
            "audio_sha256": digest(audio),
            "audio_bytes": audio.stat().st_size,
            "language": plan["language"],
            "asr_part": {
                "kind": "selected_topics",
                "input_key": plan["input_key"],
                "mappings": mappings,
            },
        }
        request_path = work / "request.json"
        write_json(request_path, request)
        if github("api", "repos/" + REPOSITORY, "--jq", ".private").strip() != "true":
            raise ControlError("Selected ASR storage must remain private")
        state["status"] = "staging_or_unknown"
        write_json(state_path, state)
        github(
            "release",
            "create",
            tag,
            str(request_path),
            str(audio),
            "--repo",
            REPOSITORY,
            "--latest=false",
            "--notes",
            "Private selected-topic GPU ASR input; no full-source ASR.",
        )
        os.environ.update(REQUEST_ID=child, INPUT_TAG=tag)
        state["status"] = "gpu_requested_or_unknown"
        write_json(state_path, state)
        controller.start("asr")
        controller.run(
            work / "result"
        )  # Frees the GPU before collecting the small JSON.
        raw = json.loads((work / "result/transcript.json").read_text())
        result = {
            "status": "complete",
            "input_key": plan["input_key"],
            "recognizer": plan["recognizer"],
            "timestamp_source": "gpu_asr",
            "gpu_request_id": child,
            "gpu_input_tag": tag,
            "clips": split_transcript(raw, mappings, plan["language"]),
        }
        write_json(output / "selected-gpu-asr.json", result)
        state.update(
            status="complete", elapsed_sec=round(time.time() - state["started_at"], 3)
        )
    finally:
        try:
            if controller.state_path.exists():
                controller.stop()
        finally:
            write_json(state_path, state)


def recover(state_file, output):
    """Retrieve an already submitted ASR result without allocating another GPU."""
    state = json.loads(state_file.read_text())
    plan = json.loads((output / "selected-gpu-asr-plan.json").read_text())
    request_id, tag = state.get("request_id", ""), state.get("input_tag", "")
    if (
        state.get("input_key") != plan["input_key"]
        or not re.fullmatch(r"[a-f0-9]{32}", request_id)
        or tag != "clipforge-input-" + request_id
    ):
        raise ControlError("Saved selected GPU request belongs to another input")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ControlError("Private GitHub input authentication is missing")
    bundles = ReleaseBundles(token)
    release = bundles.release(tag)
    if not release:
        raise ControlError("Saved GPU result is unavailable; no GPU job was repeated")
    assets = {asset["name"]: asset for asset in release["assets"]}
    if "request.json" not in assets or "cf-result-manifest.json" not in assets:
        raise ControlError(
            "Saved GPU result is not available yet; no GPU job was repeated"
        )
    work = output / "selected-gpu-recovered"
    work.mkdir(mode=0o700)
    bundles.download_asset(assets["request.json"], work / "request.json", 4 * 1024**2)
    request = json.loads((work / "request.json").read_text())
    if (
        request.get("request_id") != request_id
        or request.get("source_ref") != state.get("source_ref")
        or request.get("asr_part", {}).get("input_key") != plan["input_key"]
    ):
        raise ControlError("Saved GPU request identity differs")
    bundles.restore(tag, work / "result", request_id, required=True)
    receipt = json.loads((work / "result/request-receipt.json").read_text())
    if (
        receipt.get("request_id") != request_id
        or receipt.get("source_ref") != state.get("source_ref")
        or receipt.get("status") != "asr_part_complete"
    ):
        raise ControlError(
            "Saved GPU transcript is incomplete or belongs to another request"
        )
    raw = json.loads((work / "result/transcript.json").read_text())
    result = {
        "status": "complete",
        "input_key": plan["input_key"],
        "recognizer": plan["recognizer"],
        "timestamp_source": "gpu_asr",
        "gpu_request_id": request_id,
        "gpu_input_tag": tag,
        "clips": split_transcript(
            raw, request["asr_part"]["mappings"], plan["language"]
        ),
    }
    write_json(output / "selected-gpu-asr.json", result)
    write_json(
        output / "selected-gpu-asr-state.json",
        {**state, "status": "complete", "recovered": True},
    )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--cleanup-input", action="store_true")
    parser.add_argument("--recover", type=Path)
    parser.add_argument("--single", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stop:
        controls = [args.output / "selected-gpu-work/control"]
        controls.extend(
            (args.output / "selected-gpu-parts").glob("*/selected-gpu-work/control")
        )
        for control in controls:
            controller = Controller(control)
            if controller.state_path.exists():
                controller.stop()
    elif args.recover:
        recover(args.recover, args.output)
    elif args.cleanup_input:
        parts = args.output / "selected-gpu-parts"
        parent_state = args.output / "selected-gpu-asr-state.json"
        parent_complete = (
            parent_state.is_file()
            and json.loads(parent_state.read_text()).get("status") == "complete"
        )
        if parts.is_dir() and parent_complete:
            for part in parts.iterdir():
                if part.is_dir():
                    subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--cleanup-input",
                            "--output",
                            str(part),
                        ],
                        check=True,
                    )
        path = args.output / "selected-gpu-asr-state.json"
        if path.is_file():
            state = json.loads(path.read_text())
            tag = state.get("input_tag", "")
            if state.get("status") == "complete" and re.fullmatch(
                r"clipforge-input-[a-f0-9]{32}", tag
            ):
                github(
                    "release",
                    "delete",
                    tag,
                    "--repo",
                    REPOSITORY,
                    "--yes",
                    "--cleanup-tag",
                )
    else:
        (run if args.single else run_parallel)(args.source, args.plan, args.output)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = str(error) if isinstance(error, ControlError) else type(error).__name__
        print("Selected GPU ASR failed: " + detail, flush=True)
        raise SystemExit(1) from None
