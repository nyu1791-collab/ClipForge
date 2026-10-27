#!/usr/bin/env python3
"""Collect a completed private clip without waiting for the other clips."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clipforge.streaming_collection import collect_clip


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Existing production directory"
    )
    parser.add_argument(
        "--clip",
        type=int,
        required=True,
        help="One-based clip number shown in progress",
    )
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(collect_clip(args.output, args.clip), ensure_ascii=False))


if __name__ == "__main__":
    main()
