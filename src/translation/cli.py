"""Command line entry point."""

import argparse
import json
import os
from pathlib import Path
import sys

from clipforge.storage import PipelineError
from .pipeline import run

DEFAULTS = {
    "project": os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
    "source_language": "",
    "target_language": "日本語",
    "start": 0.0,
    "end": None,
    "workers": 6,
    "batch_size": 60,
    "model": "gemini-3.5-flash",
    "thinking": "MEDIUM",
    "height": 360,
    "encoder": "libx264",
    "preset": "veryfast",
    "font": "Noto Sans CJK JP",
    "font_size": 24,
    "line_chars": 25,
    "bilingual": False,
    "subtitles_only": False,
    "gap_recovery": True,
    "max_bytes": 6442450944,
    "max_seconds": 21600,
}


def parser():
    p = argparse.ArgumentParser(
        description="Translate overseas video speech into time-aligned subtitles"
    )
    p.add_argument("source", help="local video path or HTTPS video URL")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument(
        "--config", type=Path, help="JSON defaults; command-line flags override"
    )
    p.add_argument("--project", help="Google Cloud project ID")
    p.add_argument(
        "--source-language", help="BCP-47, e.g. en-US; omit for auto detection"
    )
    p.add_argument("--target-language")
    p.add_argument("--start", type=float, help="source start in seconds")
    p.add_argument("--end", type=float, help="source end in seconds")
    p.add_argument("--workers", type=int, choices=range(1, 7))
    p.add_argument("--batch-size", type=int)
    p.add_argument("--model")
    p.add_argument("--thinking", choices=["MINIMAL", "LOW", "MEDIUM", "HIGH"])
    p.add_argument(
        "--height", type=int, help="output height; 0 preserves source resolution"
    )
    p.add_argument("--encoder", choices=["libx264", "h264_nvenc"])
    p.add_argument("--preset")
    p.add_argument("--font")
    p.add_argument("--font-size", type=int)
    p.add_argument(
        "--line-chars", type=int, help="approximate characters per subtitle line"
    )
    p.add_argument("--bilingual", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument(
        "--subtitles-only", action=argparse.BooleanOptionalAction, default=None
    )
    p.add_argument(
        "--gap-recovery",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="retry some loud transcription gaps",
    )
    p.add_argument("--max-bytes", type=int)
    p.add_argument("--max-seconds", type=int)
    return p


def main():
    args = parser().parse_args()
    options = dict(DEFAULTS)
    if args.config:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        unknown = set(config) - set(options)
        if unknown:
            raise SystemExit(
                "Unknown configuration keys: " + ", ".join(sorted(unknown))
            )
        options.update(config)
    for key in options:
        value = getattr(args, key, None)
        if value is not None:
            options[key] = value
    if not options["project"]:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT")
    if not 1 <= options["workers"] <= 6 or not 1 <= options["batch_size"] <= 200:
        raise SystemExit("workers must be 1–6 and batch-size must be 1–200")
    if options["height"] < 0 or options["font_size"] <= 0 or options["line_chars"] <= 0:
        raise SystemExit("Invalid subtitle size or video height")
    try:
        result = run(args.source, args.output, options)
    except (PipelineError, OSError, ValueError, KeyError) as error:
        print(f"Translation failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
