#!/usr/bin/env python3
"""Local cache directories for Colab setup (uv, pip, Python, apt, Node.js)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import sys

# Standard library only: this runs on Colab before the project environment exists.
REPOSITORY = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def configuration():
    """The cache directory for this lock file, Python and platform."""
    identity = {
        "format": 2,
        "lock": sha256(REPOSITORY / "uv.lock"),
        "project": sha256(REPOSITORY / "pyproject.toml"),
        "os": {
            k: v
            for k, v in platform.freedesktop_os_release().items()
            if k in {"ID", "VERSION_ID"}
        },
        "arch": platform.machine(),
        "libc": platform.libc_ver(),
        "bootstrap_python": list(sys.version_info[:2]),
        "python_target": "3.11",
        "local_base": os.environ.get(
            "CLIPFORGE_LOCAL_CACHE", "/content/clipforge-cache"
        ),
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    root = Path(identity["local_base"]).resolve() / key
    root.mkdir(parents=True, exist_ok=True)
    return key, root


def environment(root):
    return {
        "CLIPFORGE_CACHE_ROOT": str(root),
        "UV_CACHE_DIR": str(root / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "PIP_CACHE_DIR": str(root / "pip"),
    }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["env"])
    parser.parse_args()
    _, root = configuration()
    for name, value in environment(root).items():
        print("export " + name + "=" + shlex.quote(value))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Colab cache setup failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
