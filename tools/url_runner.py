#!/usr/bin/env python3
"""Download one public archive and run ClipForge only on the private runner."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from url_common import MAX_BYTES, WORKER, identity, video_profile, youtube_url


def download_command(url, directory, metadata=False):
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-cache-dir",
        "--no-warnings",
        "--retries",
        "0",
        "--fragment-retries",
        "0",
        "--extractor-retries",
        "0",
        "--socket-timeout",
        "30",
        "--js-runtimes",
        "node",
    ]
    if metadata:
        command += ["--skip-download", "--dump-single-json"]
    else:
        command += [
            "--no-progress",
            "--max-filesize",
            str(MAX_BYTES),
            "--format",
            "bv*[height<=1080]+ba/b[height<=1080]",
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--output",
            str(directory / "source.%(ext)s"),
        ]
    return [*command, "--", url]


def run(url, root, request_id, source_ref):
    from clipforge import media
    from clipforge.models import Settings
    from clipforge.pipeline import run as pipeline
    from clipforge.storage import digest, write_json

    identity(request_id, source_ref)
    if os.environ.get("GITHUB_REPOSITORY") != WORKER:
        raise RuntimeError(
            "URL video processing requires the designated private runner"
        )
    canonical = youtube_url(url)
    if root.exists():
        raise RuntimeError(
            "Use a new output directory; URL jobs are never silently resumed"
        )
    root.mkdir(parents=True, mode=0o700)
    source_dir = root / "input"
    source_dir.mkdir()
    stage = "metadata"
    diagnostic = {
        "request_id": request_id,
        "source_ref": source_ref,
        "status": "processing",
        "human_review_performed": False,
    }
    try:
        response = subprocess.run(
            download_command(canonical, source_dir, metadata=True),
            capture_output=True,
            timeout=180,
        )
        if response.returncode or len(response.stdout) > 8 * 1024**2:
            raise RuntimeError(
                "Public video metadata could not be obtained; no alternate route attempted"
            )
        metadata = json.loads(response.stdout)
        settings = Settings.model_validate(video_profile(metadata))
        write_json(root / "effective-settings.json", settings.model_dump())
        stage = "download"
        response = subprocess.run(
            download_command(canonical, source_dir), capture_output=True, timeout=1800
        )
        source = source_dir / "source.mp4"
        if (
            response.returncode
            or not source.is_file()
            or not 0 < source.stat().st_size <= MAX_BYTES
        ):
            raise RuntimeError(
                "Public video download failed or exceeded the size limit; cookies and retries are not used"
            )
        info = media.probe(source)
        # Recheck actual media, not only remote metadata, before inference.
        actual_profile = video_profile({"duration": info["duration"]})
        if actual_profile["max_seconds"] < settings.max_seconds:
            raise RuntimeError(
                "Downloaded duration differs from inspected metadata; submit a fresh request"
            )
        diagnostic.update(
            source_sha256=digest(source),
            source_bytes=source.stat().st_size,
            duration=info["duration"],
            effective_settings=settings.model_dump(),
        )
        stage = "pipeline"
        manifest = pipeline(source, root / "result", settings)
        stage = "verification"
        rendered = manifest["rendered"]
        diagnostic.update(
            status="awaiting_human_review",
            rendered_clips=len(rendered),
            outputs=[
                {
                    "directory": r["directory"],
                    "media": r["media"],
                    "sha256": r["sha256"],
                }
                for r in rendered
            ],
        )
    except Exception as error:
        diagnostic.update(status="failed", stage=stage, error_type=type(error).__name__)
        # Only static, application-owned error types/stages enter agent context.
        failure = root / "result/failure.json"
        if failure.is_file():
            value = json.loads(failure.read_text())
            diagnostic["pipeline_failure"] = {
                k: value.get(k) for k in ("stage", "error_type")
            }
        raise
    finally:
        write_json(root / "diagnostic.json", diagnostic)
        # The input is intentionally not an artifact; never upload it or metadata URLs.
        for path in source_dir.glob("*"):
            if path.is_file():
                path.unlink()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(
            os.environ["CLIPFORGE_VIDEO_URL"],
            args.output,
            os.environ["REQUEST_ID"],
            os.environ["CLIPFORGE_SOURCE_REF"],
        )
    except Exception as error:
        print("URL validation job stopped: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
