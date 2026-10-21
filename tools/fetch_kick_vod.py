#!/usr/bin/env python3
"""Stage A: full low-bandwidth audio; stage B: precise high-quality ranges."""

import argparse
import json
from pathlib import Path

from clipforge.kick_media import KickVOD
from clipforge.storage import write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path, default=Path("workspace/cache/segments"))
    p.add_argument("--audio-only", action="store_true")
    p.add_argument("--start-time", type=float, default=0)
    end = p.add_mutually_exclusive_group()
    end.add_argument("--end-time", type=float)
    end.add_argument("--duration", type=float)
    p.add_argument("--quality", choices=["720p", "1080p", "best"], default="720p")
    p.add_argument("--max-bytes", type=int, default=2 * 1024**3)
    p.add_argument("--max-source-seconds", type=float, default=14400)
    a = p.parse_args()
    vod = None
    try:
        vod = KickVOD(a.url, a.cache, a.max_bytes, a.max_source_seconds)
        stop = a.end_time if a.duration is None else a.start_time + a.duration
        receipt = vod.retrieve(a.output, a.start_time, stop, a.quality, a.audio_only)
        write_json(a.output.with_suffix(".receipt.json"), receipt)
        print(json.dumps(receipt))
    except Exception as e:
        print(json.dumps({"status": "fetch_failed", "error_type": type(e).__name__}))
        return 1
    finally:
        if vod:
            vod.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
