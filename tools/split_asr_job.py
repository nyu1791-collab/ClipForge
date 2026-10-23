#!/usr/bin/env python3
"""Coordinate two owned Colab recognisers and publish one ordinary analysis result."""

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
    if (
        not isinstance(hashes, dict)
        or set(hashes) != {"manifest.json", "transcript.json"}
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
    if (
        settings.asr_parts != 2
        or len(plan["parts"]) != 2
        or len(part_directories) != 2
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
                request["request_id"], index, asr_split.split_key(split)
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
            parts=2,
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
    part_directories = [args.state_root / f"result-{index}" for index in range(2)]
    try:
        for index, child in enumerate(plan["parts"]):
            tag = "clipforge-input-" + child["request_id"]
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

        def recognise(index):
            child = plan["parts"][index]
            environment = {
                **os.environ,
                "REQUEST_ID": child["request_id"],
                "INPUT_TAG": "clipforge-input-" + child["request_id"],
                "CLIPFORGE_NO_MODEL_API": "1",
            }
            environment.pop("GEMINI_API_KEY", None)
            state, output = (
                args.state_root / f"control-{index}",
                part_directories[index],
            )
            try:
                control(args.control_python, state, "start", output, environment)
                control(args.control_python, state, "run", output, environment)
            finally:
                if (state / "state.json").exists():
                    control(args.control_python, state, "stop", output, environment)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(recognise, index) for index in range(2)]
            errors = []
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise PipelineError(
                    "split ASR failed; private part outputs were retained"
                ) from None
        finish(source, args.output, plan, part_directories, os.environ["GITHUB_RUN_ID"])
        print("Both ASR parts verified and merged; topics allocated once.", flush=True)
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
