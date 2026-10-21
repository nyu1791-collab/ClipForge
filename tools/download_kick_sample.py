#!/usr/bin/env python3
"""Fetch a bounded public Kick VOD sample with explicit browser compatibility."""

import argparse
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from inspect_kick_vod import VideoMetadata, browser_fetch


def required(url, limit):
    result, body = browser_fetch(url, limit)
    if body is None:
        raise ValueError("request failed: " + result["status"])
    return body


def child(base, ref, root):
    target = urljoin(base, ref)
    p = urlsplit(target)
    if (
        p.scheme != "https"
        or p.hostname != "stream.kick.com"
        or p.port not in (None, 443)
        or p.username
        or p.password
        or p.query
        or p.fragment
        or not p.path.startswith(root)
        or any(part in {".", ".."} for part in p.path.split("/"))
        or "%" in p.path
    ):
        raise ValueError("unsupported media location")
    return target


def download(url, output, seconds):
    if not re.fullmatch(
        r"https://kick\.com/[A-Za-z0-9_-]+/videos/[0-9a-fA-F]{8}"
        r"(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        url,
    ):
        raise ValueError("expected canonical Kick VOD URL")
    if not math.isfinite(seconds) or not 1 <= seconds <= 120 or output.exists():
        raise ValueError("require 1-120 seconds and a new output path")
    parser = VideoMetadata()
    parser.feed(required(url, 2 * 1024 * 1024).decode("utf-8"))
    videos = [v for v in parser.videos if v.get("url") == url]
    if len(videos) != 1:
        raise ValueError("video identity missing or ambiguous")
    master = videos[0].get("contentUrl", "")
    root = urlsplit(master).path.rsplit("/", 1)[0] + "/"
    child(master, master, root)
    lines = required(master, 256 * 1024).decode().splitlines()
    variants = []
    for index, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF:"):
            height = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if height and int(height[1]) <= 360:
                variants.append((int(height[1]), lines[index + 1]))
    if not variants:
        raise ValueError("no sample rendition at or below 360p")
    variant = child(master, max(variants)[1], root)
    playlist = required(variant, 256 * 1024).decode()
    if not playlist.startswith("#EXTM3U") or any(
        t in playlist
        for t in (
            "#EXT-X-KEY",
            "#EXT-X-BYTERANGE",
            "#EXT-X-MAP",
            "#EXT-X-DISCONTINUITY",
        )
    ):
        raise ValueError("unsupported playlist features")
    segments, duration, pending = [], 0.0, None
    for line in playlist.splitlines():
        if line.startswith("#EXTINF:"):
            pending = float(line.split(":", 1)[1].split(",")[0])
            if not math.isfinite(pending) or not 0 < pending <= 30:
                raise ValueError("invalid segment duration")
        elif line and not line.startswith("#"):
            if pending is None:
                raise ValueError("missing segment duration")
            segments.append(child(variant, line, root))
            duration += pending
            pending = None
            if duration >= seconds:
                break
    if duration < seconds or len(segments) > 120:
        raise ValueError("sample exceeds available duration or segment limit")
    with tempfile.TemporaryDirectory(prefix="clipforge-kick-") as temp:
        ts = Path(temp) / "sample.ts"
        total = 0
        with ts.open("wb") as handle:
            for segment in segments:
                body = required(
                    segment, min(16 * 1024 * 1024, 64 * 1024 * 1024 - total)
                )
                total += len(body)
                handle.write(body)
        sample = Path(temp) / "sample.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(ts),
                "-t",
                str(seconds),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(sample),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        # Exclusive destination creation preserves existing files even during a race.
        with output.open("xb") as dest, sample.open("rb") as source:
            import shutil

            shutil.copyfileobj(source, dest)
    return {
        "status": "sample_downloaded",
        "seconds_requested": seconds,
        "segments": len(segments),
        "source_bytes": total,
        "output": str(output),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--seconds", type=float, default=10)
    a = p.parse_args()
    try:
        print(json.dumps(download(a.url, a.output, a.seconds)))
        return 0
    except Exception as e:
        print(json.dumps({"status": "sample_failed", "error_type": type(e).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
