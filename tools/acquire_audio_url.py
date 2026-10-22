#!/usr/bin/env python3
"""Acquire URL-only audio on the hosted runner before allocating a GPU."""

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile

from clipforge.audio_first import acquire
from clipforge.kick_media import KickVOD, canonical
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json
from submit_job import parse_settings_json


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--settings-json", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    canonical(args.url)
    if not re.fullmatch(r"[a-f0-9]{32}", args.request_id) or not re.fullmatch(
        r"[a-f0-9]{40}", args.source_ref
    ):
        raise RuntimeError("invalid request identity or source revision")
    request = {
        "request_id": args.request_id,
        "source_ref": args.source_ref,
        "video_sha256": None,
        "video_bytes": None,
        "settings": parse_settings_json(args.settings_json),
        "chat_sha256": None,
        "audio_source_url": args.url,
    }
    settings = Settings.model_validate(request["settings"])
    if (
        settings.gemini_model != "manual-no-api"
        or settings.image_search
        or settings.table_query
        or settings.chat_url
    ):
        raise RuntimeError(
            "audio analysis cannot include model calls or external lookups"
        )
    args.directory.mkdir(parents=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=".clipforge-acquire-", dir=args.directory.parent
    ) as temporary:
        root = Path(temporary)
        vod = KickVOD(
            args.url,
            root / "segments",
            max_bytes=settings.max_input_bytes,
            max_seconds=settings.max_source_seconds,
        )
        try:
            acquire(vod, root)
        finally:
            vod.close()
        audio = root / "audio.m4a"
        size = audio.stat().st_size
        if not 0 < size <= min(settings.max_input_bytes, 2147483648):
            raise RuntimeError("acquired audio exceeds the input size limit")
        request.update(video_sha256=digest(audio), video_bytes=size)
        audio.replace(args.directory / "input-video.bin")
        write_json(args.directory / "request.json", request)
    print("Private audio input acquired; ASR has not started.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = type(error).__name__
        if isinstance(error, PipelineError):
            detail += ": " + str(error)
        print("Audio acquisition failed: " + detail, file=sys.stderr)
        raise SystemExit(1) from None
