#!/usr/bin/env python3
"""Register already-issued platform credentials via stdin; never log their values."""

import argparse
import json
import os
import secrets
import subprocess
import sys

NAMES = (
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
    "TWITCH_EVENTSUB_SECRET",
    "KICK_CLIENT_ID",
    "KICK_CLIENT_SECRET",
    "YOUTUBE_API_KEY",
    "TWITCASTING_CLIENT_ID",
    "TWITCASTING_CLIENT_SECRET",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write configured values to repository Secrets",
    )
    parser.add_argument(
        "--generate-eventsub-secret",
        action="store_true",
        help="Generate a missing webhook signing secret in memory and store it directly in GitHub",
    )
    parser.add_argument("--names", nargs="+", choices=NAMES, default=list(NAMES))
    args = parser.parse_args()
    receipt = {"repository": "nyu1791-collab/ClipForge", "secrets": {}}
    failed = False
    for name in args.names:
        if name == "TWITCH_EVENTSUB_SECRET" and args.generate_eventsub_secret:
            if not args.apply:
                receipt["secrets"][name] = "generation_requires_apply"
                failed = True
                continue
            existing = subprocess.run(
                [
                    "gh",
                    "secret",
                    "list",
                    "--repo",
                    receipt["repository"],
                    "--json",
                    "name",
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            if existing.returncode:
                receipt["secrets"][name] = "repository_access_failed"
                failed = True
                break
            if any(item["name"] == name for item in json.loads(existing.stdout)):
                receipt["secrets"][name] = "already_registered"
                continue
            value = secrets.token_hex(32)
        else:
            value = os.environ.get(name, "").strip()
        if not value or value.startswith(("YOUR_", "REPLACE_", "<")):
            receipt["secrets"][name] = "missing"
            failed = True
            continue
        if "\n" in value or "\r" in value:
            receipt["secrets"][name] = "invalid"
            failed = True
            continue
        if not args.apply:
            receipt["secrets"][name] = "available_locally_not_registered"
            continue
        result = subprocess.run(
            ["gh", "secret", "set", name, "--repo", receipt["repository"]],
            input=value,
            text=True,
            capture_output=True,
            timeout=30,
        )
        receipt["secrets"][name] = (
            "registered" if result.returncode == 0 else "registration_failed"
        )
        failed |= result.returncode != 0
        if result.returncode:
            break  # Stop on refusal. Never change identity or token to bypass it.
    print(json.dumps(receipt, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError):
        print(
            "Secret registration could not finish; inspect repository Secret names.",
            file=sys.stderr,
        )
        raise SystemExit(1)
