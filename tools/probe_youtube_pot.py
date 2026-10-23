#!/usr/bin/env python3
"""Measure Docker PO Token generation and usage without retaining token values."""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from clipforge.media_sources import source_kind
from clipforge.storage import write_json


def contains_token(value):
    if isinstance(value, dict):
        return any(
            (key == "poToken" and bool(item)) or contains_token(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(map(contains_token, value))


def worker(url, policy):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    metrics = {
        "provider_loaded": False,
        "generation_attempted": False,
        "token_generated": False,
        "player_requests_with_token": 0,
        "media_requests_with_token": 0,
        "provider_unavailable": False,
        "provider_generation_failed": False,
    }

    class Logger:
        def debug(self, message):
            # Trace output can contain complete tokens: inspect booleans only.
            metrics["provider_loaded"] |= (
                "PO Token Providers:" in message and "bgutil:" in message
            )
            metrics["generation_attempted"] |= (
                "Generating a " in message and "PO Token" in message
            )
            metrics["token_generated"] |= "Generated POT:" in message
            metrics["provider_unavailable"] |= "server is not available" in message
            metrics["provider_generation_failed"] |= (
                "Error reaching POST /get_pot" in message
                or "No PO Token providers were able" in message
            )

        info = warning = error = debug

    class ObservedDownloader(YoutubeDL):
        def urlopen(self, request):
            if hasattr(request, "data") and request.data:
                try:
                    if contains_token(json.loads(request.data)):
                        metrics["player_requests_with_token"] += 1
                except (ValueError, UnicodeError, TypeError):
                    pass
            address = getattr(request, "url", "")
            if address and parse_qs(urlsplit(address).query).get("pot"):
                metrics["media_requests_with_token"] += 1
            return super().urlopen(request)

    options = {
        "quiet": True,
        "no_warnings": True,
        "verbose": True,
        "logger": Logger(),
        "cachedir": False,
        "noplaylist": True,
        "skip_download": True,
        "retries": 0,
        "extractor_retries": 0,
        "socket_timeout": 15,
        "js_runtimes": {"node": {}},
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "mweb"],
                "fetch_pot": [policy],
                "pot_trace": ["true"],
            }
        },
    }
    if policy == "always":
        # Ensure the player API is exercised rather than reusing a webpage response.
        options["extractor_args"]["youtube"]["webpage_skip"] = ["player_response"]
    result = {"status": "metadata_failed", **metrics}
    try:
        with ObservedDownloader(options) as downloader:
            info = downloader.extract_info(url, download=False)
            result.update(
                status="metadata_available",
                duration=info.get("duration"),
                formats_count=len(info.get("formats") or []),
            )
    except DownloadError as error:
        message = str(error)
        if "Sign in to confirm" in message and "bot" in message:
            result["error"] = "YouTube requires sign-in to confirm you are not a bot"
        else:
            result["error"] = (
                "yt-dlp metadata extraction failed; raw diagnostic suppressed to protect tokens"
            )
    except Exception as error:
        result["error"] = type(error).__name__
    result.update(metrics)
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--network", choices=("direct", "warp"), default="direct")
    parser.add_argument(
        "--output", type=Path, default=Path("probe-result/docker-direct.json")
    )
    parser.add_argument("--worker", choices=("auto", "always"))
    args = parser.parse_args()
    if source_kind(args.url) != "youtube":
        parser.error("expected a YouTube URL")
    os.umask(0o077)
    os.environ.pop("YOUTUBE_COOKIES_B64", None)
    if args.worker:
        worker(args.url, args.worker)
        return
    with urlopen("http://127.0.0.1:4416/ping", timeout=5) as response:
        health = json.load(response)
    reports = []
    for policy in ("auto", "always"):
        started = time.monotonic()
        report = {
            "network": args.network,
            "clients": ["web", "mweb"],
            "fetch_pot": policy,
            "webpage_player_response_skipped": policy == "always",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "yt_dlp_version": version("yt-dlp"),
            "provider_version": version("bgutil-ytdlp-pot-provider"),
            "server_version": health.get("version"),
            "server_ping_ok": True,
            "server_uptime_at_group_start": health.get("server_uptime"),
            "cookies_used": False,
            "media_downloaded": False,
            "gpu_allocated": False,
        }
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--url",
                    args.url,
                    "--worker",
                    policy,
                ],
                capture_output=True,
                text=True,
                timeout=150,
            )
            if result.returncode == 0:
                report.update(json.loads(result.stdout))
            else:
                report.update(
                    status="probe_failed",
                    error="diagnostic worker failed; raw output suppressed",
                )
        except subprocess.TimeoutExpired:
            report.update(status="timeout", error="metadata probe exceeded 150 seconds")
        report.update(
            ended_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        reports.append(report)
        write_json(args.output, {"experiments": reports})
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
