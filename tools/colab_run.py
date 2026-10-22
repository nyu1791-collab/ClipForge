#!/usr/bin/env python3
"""Direct GPU pipeline from a local file or HTTPS file URL; optional Drive copy."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input", type=Path)
    inputs.add_argument(
        "--url", help="Direct HTTPS media file URL, not a watch/HLS page"
    )
    parser.add_argument("--gemini-model", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--drive-output", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    from clipforge.hardware import asr_options, nvenc_available
    from clipforge.models import Settings
    from clipforge.pipeline import run
    from clipforge.storage import PipelineError, digest

    config = json.loads(args.config.read_text()) if args.config else {}
    config.update(
        gemini_model=args.gemini_model,
        whisper_device="cuda",
        whisper_compute_type="float16",
    )
    settings = Settings.model_validate(config)
    asr_options(settings)  # Fail before download/API calls if no GPU is present.
    print(
        "GPU required. Video encoder:", "h264_nvenc" if nvenc_available() else "libx264"
    )
    if args.drive_output:
        if args.drive_output.exists():
            raise PipelineError(
                "Drive destination already exists; choose a new directory"
            )
        if args.drive_output.resolve().is_relative_to(args.output.resolve()):
            raise PipelineError(
                "Drive destination must be outside the local output directory"
            )
    with tempfile.TemporaryDirectory(prefix="clipforge-source-") as temporary:
        source = args.input
        if args.url:
            parsed = urlsplit(args.url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise PipelineError(
                    "use an HTTPS media URL without embedded credentials"
                )
            source = Path(temporary) / "source-video.bin"
            with urllib.request.urlopen(args.url, timeout=120) as response:
                if urlsplit(response.url).scheme != "https":
                    raise PipelineError("media URL redirected outside HTTPS")
                length = response.headers.get("Content-Length")
                if length and int(length) > settings.max_input_bytes:
                    raise PipelineError("download exceeds configured size limit")
                size = 0
                with source.open("xb") as target:
                    while data := response.read(1024 * 1024):
                        size += len(data)
                        if size > settings.max_input_bytes:
                            raise PipelineError(
                                "download exceeds configured size limit"
                            )
                        target.write(data)
            print("Media file downloaded; URL not logged.")
        result = run(source, args.output, settings, resume=args.resume)
    if result["status"] != "complete":
        raise PipelineError("pipeline did not complete; Drive copy skipped")
    if args.drive_output:
        shutil.copytree(args.output, args.drive_output)
        for source in args.output.rglob("*"):
            if source.is_file() and digest(source) != digest(
                args.drive_output / source.relative_to(args.output)
            ):
                raise PipelineError(
                    "Drive copy checksum mismatch; local outputs retained"
                )
        print("Drive copy completed and checksums matched.")
    print("Pipeline complete; human review has not been performed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        # Avoid exposing signed download URLs or provider response bodies.
        from clipforge.storage import PipelineError

        print(
            str(error) if isinstance(error, PipelineError) else type(error).__name__,
            file=sys.stderr,
        )
        raise SystemExit(1) from None
