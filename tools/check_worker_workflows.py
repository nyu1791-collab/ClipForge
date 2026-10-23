#!/usr/bin/env python3
"""Compare private worker workflows with one exact public ClipForge source commit."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

WORKFLOWS = {
    ".github/workflows/clipforge.yml": "deploy/clipforge.yml",
    ".github/workflows/clipforge-stages.yml": "deploy/audio-stages.yml",
}


def check_workflows(worker_directory, source_directory, source_ref):
    if not isinstance(source_ref, str) or not re.fullmatch(r"[a-f0-9]{40}", source_ref):
        raise ValueError("source_ref must be an exact commit SHA")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_directory,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    if head != source_ref:
        raise ValueError("checked-out source differs from source_ref")
    rows = []
    for worker_name, source_name in WORKFLOWS.items():
        paths = (
            Path(worker_directory) / worker_name,
            Path(source_directory) / source_name,
        )
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("workflow must be a regular file")
        if any(path.stat().st_size > 2 * 1024**2 for path in paths):
            raise ValueError("workflow exceeds the size limit")
        worker, source = (path.read_bytes() for path in paths)
        rows.append(
            {
                "worker": worker_name,
                "source": source_name,
                "matches": worker == source,
                "worker_sha256": hashlib.sha256(worker).hexdigest(),
                "source_sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    return {
        "status": "matched" if all(row["matches"] for row in rows) else "mismatch",
        "source_ref": source_ref,
        "files": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-directory", type=Path, required=True)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    args = parser.parse_args(argv)
    try:
        result = check_workflows(
            args.worker_directory, args.source_directory, args.source_ref
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Workflow comparison failed: invalid source identity or workflow files",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
