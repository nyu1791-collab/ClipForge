#!/usr/bin/env python3
"""Generate shorts, normal topic videos, or both from one shared source analysis."""

import argparse
import json
from pathlib import Path

from clipforge.batch import run
from clipforge.batch_models import ClipsManifest, RichStyle
from clipforge.batch_workflow import automatic
from clipforge.storage import write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest", type=Path)
    mode.add_argument("--auto", metavar="KICK_VOD_URL")
    p.add_argument("--output-kind", choices=["shorts", "topics", "both"])
    p.add_argument("--count", type=int, choices=[3, 4, 5], default=3)
    p.add_argument("--clip-seconds", type=float, default=45)
    p.add_argument("--topic-seconds", type=float, default=300)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path, default=Path("workspace/cache"))
    p.add_argument("--assets", type=Path, default=Path("assets"))
    p.add_argument("--quality", choices=["720p", "1080p", "best"], default="720p")
    p.add_argument("--whisper-model", default="small")
    p.add_argument(
        "--style", type=Path, help="RichStyle JSON for shorts or a single output kind"
    )
    p.add_argument(
        "--topic-style",
        type=Path,
        help="Separate RichStyle JSON for normal topic videos",
    )
    p.add_argument("--max-bytes", type=int, default=2 * 1024**3)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--plan-only", action="store_true")
    a = p.parse_args()
    try:
        if a.auto:
            result = automatic(
                a.auto,
                a.output,
                a.cache,
                a.assets,
                a.output_kind or "shorts",
                a.count,
                a.clip_seconds,
                a.topic_seconds,
                a.quality,
                a.whisper_model,
                a.style,
                a.topic_style,
                a.resume,
                a.plan_only,
                a.max_bytes,
            )
        else:
            manifest = ClipsManifest.model_validate_json(a.manifest.read_text())
            if a.output_kind and a.output_kind != manifest.output_kind:
                raise ValueError(
                    "manifest output kind must match; use separate manifests for both"
                )
            style = (
                a.topic_style
                if manifest.output_kind == "topics" and a.topic_style
                else a.style
            )
            if style:
                manifest.style = RichStyle.model_validate_json(style.read_text())
            if a.plan_only:
                if a.output.exists():
                    raise ValueError("plan destination exists")
                write_json(a.output, manifest.model_dump())
                result = {
                    "status": "planned",
                    "clips": len(manifest.clips),
                    "output_kind": manifest.output_kind,
                }
            else:
                rendered = run(
                    manifest,
                    a.output,
                    a.cache,
                    a.assets,
                    a.quality,
                    a.whisper_model,
                    a.resume,
                    a.max_bytes,
                )
                result = {
                    "status": rendered["status"],
                    "clips": len(rendered["clips"]),
                    "output_kind": manifest.output_kind,
                    "human_review_performed": False,
                }
        print(json.dumps(result))
        return 0
    except Exception as e:
        # Paths, provider bodies, playback URLs and transcripts stay out of logs.
        print(json.dumps({"status": "batch_failed", "error_type": type(e).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
