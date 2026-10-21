"""Command-line interface for ClipForge."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from clipforge.cut import cut_clip


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipforge")
    subcommands = parser.add_subparsers(dest="command", required=True)

    cut_parser = subcommands.add_parser("cut", help="cut one explicit time range from a local video")
    cut_parser.add_argument("--input", required=True, type=Path, help="source video; never modified")
    cut_parser.add_argument("--output", required=True, type=Path, help="new .mp4 path; must not exist")
    cut_parser.add_argument("--start", required=True, type=float, help="start time in seconds")
    cut_parser.add_argument("--end", required=True, type=float, help="end time in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = cut_clip(args.input, args.output, args.start, args.end)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0
