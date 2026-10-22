#!/usr/bin/env python3
"""Configure local public runtime assets and release cache identities."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
PARTS = ("uv", "pip", "python", "apt", "bootstrap")


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


def profile():
    value = os.environ.get(
        "CLIPFORGE_CACHE_PROFILE",
        "cpu" if os.environ.get("CLIPFORGE_CPU_ONLY") == "1" else "gpu",
    )
    if value not in {"cpu", "gpu"}:
        raise ValueError("Invalid cache profile: use cpu or gpu")
    return value


def release_parts():
    return PARTS if profile() == "cpu" else (*PARTS, "models/hub")


def release_tag(key):
    return f"clipforge-cache-{profile()}-{key}"


def configuration():
    identity = {
        "format": 1,
        "profile": profile(),
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
    # Keep the tuple shape consumed by the automatic remote worker.
    return key, root, None


def environment(root):
    return {
        "CLIPFORGE_CACHE_ROOT": str(root),
        "UV_CACHE_DIR": str(root / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "PIP_CACHE_DIR": str(root / "pip"),
        "HF_HUB_CACHE": str(root / "models" / "hub"),
    }


def mark_restored(key, root):
    (root / ".restored").write_text(key)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["env"])
    parser.parse_args()
    _, root, _ = configuration()
    for name, value in environment(root).items():
        print("export " + name + "=" + shlex.quote(value))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Avoid printing network bodies or authentication-related exception details.
        print("Colab asset cache failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
