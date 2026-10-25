#!/usr/bin/env python3
"""Acquire URL-only audio on the hosted runner before allocating a GPU."""

import argparse
from contextlib import contextmanager
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from clipforge.audio_first import acquire
from clipforge.kick_media import KickVOD
from clipforge.media_sources import audio_only_url, canonical, open_source
from clipforge.models import Settings
from clipforge.storage import PipelineError, digest, write_json
from submit_job import parse_settings_json


@contextmanager
def rejected(report, reason):
    """Record a stable reason code for a refused input and let the error propagate."""
    try:
        yield
    except Exception:
        report["reason"] = reason
        raise


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--settings-json", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--require-video", action="store_true")
    parser.add_argument("--cookies-file", type=Path)
    parser.add_argument(
        "--youtube-web-clients",
        action="store_true",
        help="Use web/mweb with explicit PO Token requests; requires an external provider",
    )
    parser.add_argument("--verified-audio-dir", type=Path)
    parser.add_argument("--verified-audio-run", type=int)
    parser.add_argument("--verified-audio-request")
    parser.add_argument("--verified-audio-source-ref")
    args = parser.parse_args(argv)
    reuse_fields = (
        args.verified_audio_dir,
        args.verified_audio_run,
        args.verified_audio_request,
        args.verified_audio_source_ref,
    )
    # The report exists before any validation, so refused inputs leave a reason code too.
    args.directory.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    report = {
        "schema": 1,
        "source_sha256": hashlib.sha256(args.url.encode()).hexdigest(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "acquisition_failed",
        "phase": "input_validation",
        "gpu_allocated": False,
        "asr_started": False,
        "transport": None,
    }
    try:
        with rejected(report, "invalid_reuse"):
            if any(value is not None for value in reuse_fields) and not all(
                value is not None for value in reuse_fields
            ):
                raise PipelineError(
                    "audio reuse requires directory, producer run, request and source revision"
                )
        with rejected(report, "invalid_url"):
            canonical(args.url)
        with rejected(report, "audio_only_source"):
            if args.require_video and audio_only_url(args.url):
                raise PipelineError(
                    "audio-only sources cannot produce automatic video previews"
                )
        with rejected(report, "request_mismatch"):
            if not re.fullmatch(r"[a-f0-9]{32}", args.request_id) or not re.fullmatch(
                r"[a-f0-9]{40}", args.source_ref
            ):
                raise RuntimeError("invalid request identity or source revision")
        report.update(request_id=args.request_id, source_ref=args.source_ref)
        with rejected(report, "invalid_settings"):
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
        report["transport"] = settings.youtube_audio_transport
        if (
            settings.youtube_audio_transport == "warp"
            and args.verified_audio_dir is None
        ):
            from clipforge.youtube_transport import verify_ready

            report["phase"] = "route_before"
            verify_ready(args.url)
            args.youtube_web_clients = True
        with tempfile.TemporaryDirectory(
            prefix=".clipforge-acquire-", dir=args.directory.parent
        ) as temporary:
            root = Path(temporary)
            reused = None
            if args.verified_audio_dir is not None:
                from clipforge.media_sources import verify_audio_artifact

                report["phase"] = "reuse_verify"
                with rejected(report, "invalid_reuse_artifact"):
                    reused = verify_audio_artifact(
                        args.verified_audio_dir,
                        source_url=args.url,
                        source_ref=args.verified_audio_source_ref,
                        request_id=args.verified_audio_request,
                        workflow_run=args.verified_audio_run,
                        max_bytes=settings.max_input_bytes,
                        max_seconds=settings.max_source_seconds,
                    )
                if args.require_video and not reused["source_has_video"]:
                    raise PipelineError(
                        "audio-only sources cannot produce automatic video previews"
                    )
                shutil.copyfile(
                    args.verified_audio_dir / "audio.m4a", root / "audio.m4a"
                )
                report.update(
                    transport="verified_artifact",
                    producer_run=reused["workflow_run"],
                    producer_request=reused["request_id"],
                    producer_source_ref=reused["source_ref"],
                    source_has_video=reused["source_has_video"],
                )
            else:
                report["phase"] = "acquire"
                vod = open_source(
                    args.url,
                    root / "segments",
                    kick_factory=KickVOD,
                    **(
                        {"cookies_file": args.cookies_file} if args.cookies_file else {}
                    ),
                    **(
                        {"youtube_web_clients": True}
                        if args.youtube_web_clients
                        else {}
                    ),
                    max_bytes=settings.max_transport_bytes,
                    max_seconds=settings.max_source_seconds,
                )
                try:
                    acquire(vod, root)
                    report["source_has_video"] = bool(getattr(vod, "has_video", True))
                    if settings.youtube_audio_transport == "warp":
                        verify_ready(args.url)
                    if args.require_video and not getattr(vod, "has_video", True):
                        raise PipelineError(
                            "audio-only sources cannot produce automatic video previews"
                        )
                finally:
                    vod.close()
            audio = root / "audio.m4a"
            report["phase"] = "output_validation"
            if reused is not None and digest(audio) != reused["audio_sha256"]:
                raise PipelineError("reused audio changed during copying")
            size = audio.stat().st_size
            if not 0 < size <= min(settings.max_input_bytes, 2147483648):
                raise RuntimeError("acquired audio exceeds the input size limit")
            request.update(video_sha256=digest(audio), video_bytes=size)
            audio.replace(args.directory / "input-video.bin")
            write_json(args.directory / "request.json", request)
        report.update(
            status="audio_acquired",
            phase="complete",
            audio_sha256=request["video_sha256"],
            audio_bytes=request["video_bytes"],
        )
    except Exception as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        report.update(
            ended_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        write_json(args.directory / "acquisition-report.json", report)
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
