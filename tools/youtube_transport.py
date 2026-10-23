#!/usr/bin/env python3
"""Start, verify and stop owned YouTube transport before allocating a GPU."""

import argparse
import json
from pathlib import Path
import sys

from clipforge.youtube_transport import Transport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("setup", "prepare", "connect", "verify", "cleanup")
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--url")
    args = parser.parse_args()
    transport = Transport(args.state_dir, args.url)
    if args.action in {"setup", "prepare"} and transport.state is not None:
        print(
            "YouTube transport state already exists; preserving owned resources",
            file=sys.stderr,
        )
        return 1
    try:
        if args.action in {"setup", "prepare"}:
            transport.prepare()
        if args.action in {"setup", "connect"}:
            transport.connect()
        if args.action == "verify":
            transport.verify()
        if args.action == "cleanup":
            transport.cleanup()
    except Exception as error:
        if transport.state is not None:
            transport.state.setdefault(
                "failure",
                {
                    "phase": transport.state.get("phase"),
                    "error_type": type(error).__name__,
                },
            )
            transport.save()
            if args.action != "cleanup":
                try:
                    transport.cleanup()
                except Exception:
                    pass
        print("YouTube transport failed: " + type(error).__name__, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": (transport.state or {}).get("status", "absent"),
                "timings": (transport.state or {}).get("timings", {}),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
