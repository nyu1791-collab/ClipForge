#!/usr/bin/env python3
"""Download named editorial JSON assets through the private GitHub REST API."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPO = "nyu1791-collab/ai-model-workers"
TAG = re.compile(
    r"clipforge-kick-editorial-(?:(?:replay-)?[0-9]+|revision-[a-f0-9]{32})"
)
NAME = re.compile(r"[A-Za-z0-9_.-]+(?:\.json|\.prompt\.txt)")
MAX_ASSET = 8 * 1024**2


def api(*args):
    result = subprocess.run(["gh", "api", *args], capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError("Private editorial metadata request failed")
    return json.loads(result.stdout)


def download(tag, output, names=None):
    if not TAG.fullmatch(tag):
        raise ValueError("Invalid editorial release")
    if api(f"repos/{REPO}").get("private") is not True:
        raise RuntimeError("Editorial repository must be private")
    release = api(f"repos/{REPO}/releases/tags/{tag}")
    assets = release.get("assets", [])
    if names is None:
        selected = [item for item in assets if NAME.fullmatch(item.get("name", ""))]
        if "audio-editor-receipt.json" not in {item["name"] for item in selected}:
            raise RuntimeError("Editorial receipt is missing")
    else:
        if any(not NAME.fullmatch(name) for name in names) or len(set(names)) != len(
            names
        ):
            raise ValueError("Invalid editorial asset name")
        selected = [item for item in assets if item.get("name") in names]
        if len(selected) != len(names) or {item["name"] for item in selected} != set(
            names
        ):
            raise RuntimeError("Editorial assets missing or ambiguous")
    if (
        len(selected) > 2000
        or len({item["name"] for item in selected}) != len(selected)
        or any(type(item.get("size")) is not int for item in selected)
        or sum(item.get("size", MAX_ASSET + 1) for item in selected) > 500 * 1024**2
    ):
        raise RuntimeError("Editorial asset set exceeds the download limit")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in selected:
        asset_id, size, name = item.get("id"), item.get("size"), item["name"]
        if (
            type(asset_id) is not int
            or asset_id <= 0
            or type(size) is not int
            or not 0 < size <= MAX_ASSET
        ):
            raise RuntimeError("Invalid editorial asset metadata")
        path = output / name
        created = False
        try:
            with path.open("xb") as target:
                created = True
                result = subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/{REPO}/releases/assets/{asset_id}",
                        "-H",
                        "Accept: application/octet-stream",
                    ],
                    stdout=target,
                    stderr=subprocess.PIPE,
                    timeout=600,
                )
            if result.returncode or path.stat().st_size != size:
                raise RuntimeError("Editorial asset download failed or size differs")
            expected = item.get("digest")
            if (
                expected
                and expected
                != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            ):
                raise RuntimeError("Editorial asset digest differs")
            path.chmod(0o600)
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    download(args.tag, args.output, args.names or None)


if __name__ == "__main__":
    main()
