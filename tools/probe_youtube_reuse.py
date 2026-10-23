#!/usr/bin/env python3
"""Prove that verified audio stages a fresh request after transport shutdown."""

import argparse
import json
import os
from pathlib import Path
import time
import uuid

from acquire_audio_url import main as acquire_input
from clipforge.storage import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    identity = uuid.uuid4().hex
    started = time.monotonic()
    settings = {
        "gemini_model": "manual-no-api",
        "asr_parts": 2,
        "youtube_audio_transport": "warp",
    }
    acquire_input(
        [
            "--url",
            args.url,
            "--request-id",
            identity,
            "--source-ref",
            os.environ["SOURCE_REF"],
            "--settings-json",
            json.dumps(settings),
            "--directory",
            str(args.output),
            "--verified-audio-dir",
            str(args.directory),
            "--verified-audio-run",
            os.environ["GITHUB_RUN_ID"],
            "--verified-audio-request",
            os.environ["REQUEST_ID"],
            "--verified-audio-source-ref",
            os.environ["SOURCE_REF"],
        ]
    )
    acquisition = json.loads((args.output / "acquisition-report.json").read_text())
    if (
        acquisition["transport"] != "verified_artifact"
        or acquisition["request_id"] == os.environ["REQUEST_ID"]
    ):
        raise RuntimeError("audio reuse did not stage a fresh verified request")
    report = {
        "reuse_verified": True,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "audio_sha256": acquisition["audio_sha256"],
        "source_run": int(os.environ["GITHUB_RUN_ID"]),
        "source_request": os.environ["REQUEST_ID"],
        "new_request": identity,
        "media_downloaded": False,
        "gpu_allocated": False,
        "asr_started": False,
    }
    write_json(Path("probe-result/reuse.json"), report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
