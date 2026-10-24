"""Private output files, bounded subprocesses and verified per-stage caches."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid


# Interval ends may exceed a probed duration by millisecond rounding; accept up to this many seconds.
TIME_TOLERANCE = 0.01


class PipelineError(RuntimeError):
    pass


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            sha.update(block)
    return sha.hexdigest()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        # Created private, so the content is never readable by others while it is written.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def command_error(command, stage=None, returncode=None, timed_out=False):
    # Only the executable name, stage and exit state: arguments may hold paths, URLs or tokens.
    where = "" if stage is None else " at stage " + stage
    if timed_out:
        text = "timed out" + where
    elif returncode is None:
        text = "failed" + where
    else:
        text = "failed" + where + " (exit " + str(returncode) + ")"
    error = PipelineError(Path(str(command[0])).name + " " + text)
    error.stage = stage
    error.returncode = returncode
    error.timed_out = timed_out
    return error


def failure_fields(error):
    if not hasattr(error, "timed_out"):
        return {}
    return {
        "command_stage": error.stage,
        "returncode": error.returncode,
        "timed_out": error.timed_out,
    }


def process(command, timeout=180, stage=None, cwd=None):
    timed_out = False
    try:
        result = subprocess.run(
            [str(c) for c in command], capture_output=True, timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired:
        # Handled outside the except block: TimeoutExpired carries the full command line.
        timed_out = True
    if timed_out:
        raise command_error(command, stage, timed_out=True)
    if result.returncode:
        # FFmpeg and providers can echo paths, URLs and content. Do not echo bodies.
        raise command_error(command, stage, returncode=result.returncode)
    return result


def stage(root, name, key, producer):
    path = root / (name + ".json")
    receipt = root / ".state" / (name + ".json")
    if path.is_file() and receipt.is_file():
        try:
            old = json.loads(receipt.read_text())
            if (
                isinstance(old, dict)
                and old.get("key") == key
                and old.get("sha256") == digest(path)
            ):
                return json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A damaged cache is not a completed stage. Recompute this stage only.
            pass
    value = producer()
    write_json(path, value)
    write_json(receipt, {"key": key, "sha256": digest(path)})
    return value


def within(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise PipelineError("output path escapes the selected output directory")
    return path


def prepare_destination(destination, error_type=PipelineError):
    """Reserve a new download location without accepting symlink aliases or overwrites."""
    destination = Path(destination).absolute()
    if destination.exists() or any(
        part.is_symlink() for part in [destination, *destination.parents]
    ):
        raise error_type(
            "download destination already exists or uses a symlink; choose a new directory"
        )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return destination


def plain_tree(directory, error_type=PipelineError, description="result"):
    """List regular files/directories without following symlinks or special entries."""
    directory = Path(directory)
    files, dirs = set(), set()
    if directory.is_symlink():
        raise error_type(description + " contains a symlink")
    for current, dirnames, filenames in os.walk(directory):
        for name in dirnames + filenames:
            if Path(current, name).is_symlink():
                raise error_type(description + " contains a symlink")
        for name in dirnames:
            dirs.add(Path(current, name).relative_to(directory).as_posix())
        for name in filenames:
            path = Path(current, name)
            if not path.is_file():
                raise error_type(description + " contains a non-regular entry")
            files.add(path.relative_to(directory).as_posix())
    return files, dirs
