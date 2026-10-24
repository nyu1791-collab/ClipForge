#!/usr/bin/env python3
"""Verify full audio acquisition using the pipeline, before any ASR allocation."""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import os
import json
from pathlib import Path
import time

from clipforge.audio_first import audio_info
from clipforge.storage import PipelineError, digest, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.pop("YOUTUBE_COOKIES_B64", None)
    args.directory.mkdir(mode=0o700, parents=True)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "audio_failed",
        "yt_dlp_version": version("yt-dlp"),
        "cookies_used": False,
        "media_downloaded": False,
        "gpu_allocated": False,
        "asr_started": False,
    }
    started = time.monotonic()
    try:
        from acquire_audio_url import main as acquire_input

        settings = json.loads(
            (
                Path(__file__).resolve().parents[1] / "config.landscape.example.json"
            ).read_text()
        )
        settings.update(
            youtube_audio_transport="warp",
            asr_parts=2,
            max_transport_bytes=512 * 1024**2,
            max_source_seconds=21600,
        )
        staged = args.directory / "staged"
        phase = time.monotonic()
        acquire_input(
            [
                "--url",
                args.url,
                "--request-id",
                os.environ["REQUEST_ID"],
                "--source-ref",
                os.environ["SOURCE_REF"],
                "--settings-json",
                json.dumps(settings),
                "--directory",
                str(staged),
            ]
        )
        report["acquire_seconds"] = round(time.monotonic() - phase, 3)
        (staged / "input-video.bin").replace(args.directory / "audio.m4a")
        request = json.loads((staged / "request.json").read_text())
        audio = args.directory / "audio.m4a"
        report.update(
            media_downloaded=True,
            audio_bytes=audio.stat().st_size,
            audio_sha256=digest(audio),
            audio_info=audio_info(audio),
        )
        acquisition = json.loads((staged / "acquisition-report.json").read_text())
        write_json(
            args.directory / "audio-source.json",
            {
                "schema": 1,
                "request_id": request["request_id"],
                "source_ref": request["source_ref"],
                "workflow_run": int(os.environ["GITHUB_RUN_ID"]),
                "source_url": args.url,
                "audio_sha256": request["video_sha256"],
                "audio_bytes": request["video_bytes"],
                "audio_info": report["audio_info"],
                "source_has_video": acquisition["source_has_video"],
                "asr_started": False,
            },
        )
        report["status"] = "audio_acquired"
    except PipelineError as error:
        report["error"] = str(error)
    except Exception as error:
        report["error"] = type(error).__name__
    finally:
        canonical_receipt = args.directory / "staged/acquisition-report.json"
        if canonical_receipt.is_file():
            write_json(
                Path("probe-result/canonical-acquisition.json"),
                json.loads(canonical_receipt.read_text()),
            )
    report.update(
        ended_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    write_json(Path("probe-result/audio.json"), report)
    if os.environ.get("GITHUB_OUTPUT") and report["status"] == "audio_acquired":
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write("audio_acquired=true\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
