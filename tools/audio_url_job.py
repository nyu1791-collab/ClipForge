#!/usr/bin/env python3
"""Acquire and analyze Kick audio on the Colab worker without a model API."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from clipforge import audio_first
from clipforge.kick_media import KickVOD, canonical
from clipforge.models import Settings
from clipforge.storage import digest, write_json


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workflow-run", required=True)
    args = parser.parse_args(argv)
    canonical(args.url)
    output = args.output.resolve()
    config = Path(__file__).resolve().parents[1] / "config.landscape.example.json"
    settings = Settings.model_validate_json(config.read_text()).model_copy(
        update={"transcript_cache": str(output / "transcripts")}
    )
    with audio_first.exclusive(output):
        segments = output / "segments"
        vod = None
        try:
            vod = KickVOD(
                args.url,
                segments,
                max_bytes=settings.max_input_bytes,
                max_seconds=settings.max_source_seconds,
            )
            audio_first.acquire(vod, output)
            audio = output / "audio.m4a"
            analysis, _ = audio_first.allocate_audio(audio, output, settings, args.url)
            result = {**analysis, "rendered": [], "source_sha256": digest(audio)}
            write_json(output / "manifest.json", result)
            cache = json.loads((output / "asr-receipt.json").read_text())
            names = [
                "audio.m4a",
                "manifest.json",
                "topics.json",
                "asr-receipt.json",
                "transcripts/" + cache["key"] + ".json",
            ]
            write_json(
                output / "request-receipt.json",
                {
                    "request_id": args.request_id,
                    "source_ref": args.source_ref,
                    "workflow_run": args.workflow_run,
                    "status": result["status"],
                    "rendered_clips": 0,
                    "audio_source_url": args.url,
                    "sha256": {name: digest(output / name) for name in names},
                },
            )
        finally:
            try:
                if vod is not None:
                    vod.close()
            finally:
                shutil.rmtree(segments, ignore_errors=True)
    print(json.dumps({"status": result["status"], "clips": 0}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Audio URL job failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
