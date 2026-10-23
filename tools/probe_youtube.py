#!/usr/bin/env python3
"""Bounded anonymous metadata experiments; never download media or emit tokens."""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from clipforge.media_sources import provider_error, source_kind
from clipforge.storage import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("probe-result/metadata.json")
    )
    args = parser.parse_args()
    if source_kind(args.url) != "youtube":
        parser.error("expected a YouTube URL")
    os.umask(0o077)
    environment = os.environ.copy()
    environment.pop("YOUTUBE_COOKIES_B64", None)
    clients = ["--extractor-args", "youtube:player-client=ios,mweb,tv_embedded"]
    variants = [
        ("default", []),
        ("clients", clients),
        ("chrome", ["--impersonate", "chrome"]),
        ("clients_chrome", [*clients, "--impersonate", "chrome"]),
    ]
    reports = []
    for plugin in (False, True):
        for name, extra in variants:
            report = {
                "condition": ("po_" if plugin else "") + name,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "yt_dlp_version": version("yt-dlp"),
                "cookies_used": False,
                "media_downloaded": False,
                "gpu_allocated": False,
            }
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--ignore-config",
                "--no-cache-dir",
                "--no-playlist",
                "--skip-download",
                "--dump-single-json",
                "--verbose",
                "--socket-timeout",
                "15",
                "--retries",
                "0",
                "--extractor-retries",
                "0",
                "--js-runtimes",
                "node",
            ]
            if not plugin:
                command += ["--no-plugin-dirs"]
            command += [*extra, args.url]
            started = time.monotonic()
            try:
                result = subprocess.run(
                    command, env=environment, capture_output=True, text=True, timeout=75
                )
                diagnostic = result.stderr
                report.update(
                    exit_code=result.returncode,
                    status="metadata_available"
                    if result.returncode == 0
                    else "metadata_failed",
                )
                if result.returncode == 0:
                    metadata = json.loads(result.stdout)
                    report["duration"] = metadata.get("duration")
                    report["formats_count"] = len(metadata.get("formats") or [])
                else:
                    errors = [
                        line
                        for line in diagnostic.splitlines()
                        if line.startswith("ERROR:")
                    ]
                    report["error"] = provider_error(
                        "\n".join(errors) or diagnostic[-1500:]
                    )
                report["po_provider_loaded"] = any(
                    "PO Token Providers:" in line and "bgutil:" in line
                    for line in diagnostic.splitlines()
                )
                report["po_generation_attempted"] = any(
                    "Generating" in line and "PO Token" in line
                    for line in diagnostic.splitlines()
                )
                report["po_generation_reported_success"] = any(
                    ("Generated" in line or "successfully" in line)
                    and "PO Token" in line
                    for line in diagnostic.splitlines()
                )
            except subprocess.TimeoutExpired:
                report.update(
                    status="timeout", error="metadata probe exceeded 75 seconds"
                )
            report.update(
                ended_at=datetime.now(timezone.utc).isoformat(),
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            reports.append(report)
            write_json(args.output, {"experiments": reports})
            print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
