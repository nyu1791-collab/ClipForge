#!/usr/bin/env python3
"""Verify full audio acquisition using the pipeline, before any ASR allocation."""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import os
from pathlib import Path
import time

from clipforge.audio_first import acquire, audio_info
from clipforge.media_sources import RemoteMedia
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
    source = RemoteMedia(
        args.url,
        args.directory / "segments",
        max_bytes=512 * 1024**2,
        max_seconds=21600,
        youtube_web_clients=True,
    )
    try:
        phase = time.monotonic()
        report["metadata"] = source.metadata()
        report["metadata_seconds"] = round(time.monotonic() - phase, 3)
        phase = time.monotonic()
        acquire(source, args.directory)
        report["acquire_seconds"] = round(time.monotonic() - phase, 3)
        audio = args.directory / "audio.m4a"
        report.update(
            status="audio_acquired",
            media_downloaded=True,
            audio_bytes=audio.stat().st_size,
            audio_sha256=digest(audio),
            audio_info=audio_info(audio),
        )
    except PipelineError as error:
        report["error"] = str(error)
    except Exception as error:
        report["error"] = type(error).__name__
    finally:
        source.close()
    report.update(
        ended_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    write_json(Path("probe-result/audio.json"), report)
    if os.environ.get("GITHUB_OUTPUT") and report["status"] == "audio_acquired":
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write("audio_acquired=true\n")
    import json

    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
