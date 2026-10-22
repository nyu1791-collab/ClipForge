#!/usr/bin/env python3
"""Cache public runtime assets only; never include runner credentials or job data."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import uuid

REPOSITORY = Path(__file__).resolve().parents[1]
PARTS = ("uv", "pip", "python", "apt", "bootstrap")
RUNNER_VERSION = "2.338.0"
RUNNER_SHA256 = "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32"


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
    persistent = os.environ.get("CLIPFORGE_PERSISTENT_CACHE")
    persistent = Path(persistent).resolve() if persistent else None
    if persistent and persistent.is_relative_to(Path("/content/drive")):
        if not os.path.ismount("/content/drive"):
            raise RuntimeError("Mount Google Drive before enabling persistent caching.")
    root.mkdir(parents=True, exist_ok=True)
    if persistent:
        persistent.mkdir(parents=True, exist_ok=True)
    return key, root, persistent


def environment(root, persistent):
    return {
        "CLIPFORGE_CACHE_ROOT": str(root),
        "UV_CACHE_DIR": str(root / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "PIP_CACHE_DIR": str(root / "pip"),
        # Only model files, not HF_HOME or its authentication token, go to Drive.
        "HF_HUB_CACHE": str((persistent or root) / "models" / "hub"),
    }


def inventory(root):
    records = []
    for name in PARTS:
        for path in sorted((root / name).rglob("*")):
            if path.name.endswith(".lock") or (path.is_dir() and not path.is_symlink()):
                continue
            stat = path.lstat()
            records.append(
                (
                    str(path.relative_to(root)),
                    stat.st_size,
                    stat.st_mtime_ns,
                    os.readlink(path) if path.is_symlink() else "",
                )
            )
    return hashlib.sha256(json.dumps(records).encode()).hexdigest()


def mark_restored(key, root):
    (root / ".restored").write_text(key)
    (root / ".saved-inventory").write_text(inventory(root))


def restore(key, root, persistent):
    if not persistent or (root / ".restored").is_file():
        return
    manifest = persistent / (key + ".json")
    if not manifest.is_file():
        print("No saved dependency cache; first setup will download missing assets.")
        return
    data = json.loads(manifest.read_text())
    if data.get("key") != key or not isinstance(data.get("sha256"), str):
        raise RuntimeError("Dependency cache manifest is invalid.")
    filename = key + "-" + data["sha256"] + ".tar"
    if data.get("archive") != filename or not re.fullmatch(
        r"[a-f0-9]{64}", data["sha256"]
    ):
        raise RuntimeError("Unexpected dependency cache archive.")
    # The archive name embeds its SHA-256 and is replaced atomically on save, so the
    # file verified here is the one extracted below; it is read in place, never copied.
    archive = persistent / filename
    if sha256(archive) != data["sha256"]:
        raise RuntimeError("Dependency cache checksum mismatch; cache not restored.")
    with tempfile.TemporaryDirectory(prefix="restore-", dir=root.parent) as temporary:
        staging = Path(temporary) / "assets"
        staging.mkdir()
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if Path(member.name).parts[0] not in PARTS:
                    raise RuntimeError("Unexpected cache member.")
            bundle.extractall(staging, filter="data")
        for name in PARTS:
            if (staging / name).exists() and not (root / name).exists():
                shutil.move(staging / name, root / name)
    mark_restored(key, root)
    print(
        "Dependency cache restored and SHA-256 verified; asset transfer was required."
    )


def save(key, root, persistent):
    if not persistent:
        print("No persistent cache configured; runtime assets remain local.")
        return
    before = inventory(root)
    previous_archive = None
    previous_manifest = persistent / (key + ".json")
    if previous_manifest.is_file():
        previous_archive = json.loads(previous_manifest.read_text()).get("archive")
    state = root / ".saved-inventory"
    if (
        state.is_file()
        and state.read_text() == before
        and (persistent / (key + ".json")).is_file()
    ):
        print("Dependency cache unchanged; snapshot write skipped.")
        return

    def safe_member(info):
        if info.name.endswith(".lock"):
            return None
        if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
            raise RuntimeError("Unsupported dependency cache entry.")
        if info.issym():
            source = root / info.name
            target = source.resolve()
            if not target.is_relative_to(root):
                raise RuntimeError("Dependency cache link points outside the cache.")
            info.linkname = os.path.relpath(target, source.parent)
        return info

    with tempfile.TemporaryDirectory(prefix="save-", dir=root.parent) as temporary:
        archive = Path(temporary) / "assets.tar"
        # Uncompressed archives avoid spending GPU time compressing multi-GB CUDA wheels.
        with tarfile.open(archive, "w") as bundle:
            for name in PARTS:
                if (root / name).exists():
                    bundle.add(root / name, arcname=name, filter=safe_member)
        checksum = sha256(archive)
        filename = key + "-" + checksum + ".tar"
        destination = persistent / filename
        if not destination.exists():
            pending = persistent / (filename + ".partial-" + uuid.uuid4().hex)
            try:
                shutil.copyfile(archive, pending)
                pending.replace(destination)
            finally:
                pending.unlink(missing_ok=True)
        manifest = persistent / (key + ".json.partial-" + uuid.uuid4().hex)
        manifest.write_text(
            json.dumps({"key": key, "archive": filename, "sha256": checksum})
        )
        manifest.replace(persistent / (key + ".json"))
    state.write_text(before)
    (root / ".restored").write_text(key)
    if (
        previous_archive
        and previous_archive != filename
        and re.fullmatch(key + r"-[a-f0-9]{64}\.tar", previous_archive)
    ):
        (persistent / previous_archive).unlink(missing_ok=True)
    print(
        "Dependency cache saved. Model files are persisted separately; no job data was included."
    )


def runner_asset(root, persistent):
    name = f"actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz"
    destination = root / name
    saved = persistent / "runner-assets" / name if persistent else None
    if not destination.is_file():
        temporary = root / (name + ".partial-" + uuid.uuid4().hex)
        try:
            if saved and saved.is_file():
                shutil.copyfile(saved, temporary)
            else:
                if os.environ.get("CLIPFORGE_OFFLINE") == "1":
                    raise RuntimeError(
                        "Runner is not cached; offline mode forbids downloading."
                    )
                url = f"https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/{name}"
                with (
                    urllib.request.urlopen(url, timeout=120) as response,
                    temporary.open("xb") as out,
                ):
                    shutil.copyfileobj(response, out, 1024 * 1024)
            if sha256(temporary) != RUNNER_SHA256:
                raise RuntimeError("GitHub runner checksum mismatch.")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    if sha256(destination) != RUNNER_SHA256:
        raise RuntimeError("Cached GitHub runner checksum mismatch.")
    if saved and not saved.is_file():
        saved.parent.mkdir(parents=True, exist_ok=True)
        temporary = saved.with_name(saved.name + ".partial-" + uuid.uuid4().hex)
        try:
            shutil.copyfile(destination, temporary)
            temporary.replace(saved)
        finally:
            temporary.unlink(missing_ok=True)
    print(destination)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["env", "github-env", "restore", "save", "runner"]
    )
    args = parser.parse_args()
    key, root, persistent = configuration()
    if args.command == "env":
        for name, value in environment(root, persistent).items():
            print("export " + name + "=" + shlex.quote(value))
    elif args.command == "github-env":
        with Path(os.environ["GITHUB_ENV"]).open("a") as out:
            for name, value in environment(root, persistent).items():
                if "\n" in value or "\r" in value:
                    raise RuntimeError("Invalid cache path.")
                out.write(name + "=" + value + "\n")
    else:
        with (root / ".cache.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == "restore":
                restore(key, root, persistent)
            elif args.command == "save":
                save(key, root, persistent)
            else:
                runner_asset(root, persistent)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Avoid printing network bodies or authentication-related exception details.
        print("Colab asset cache failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
