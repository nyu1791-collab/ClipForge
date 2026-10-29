"""Bound GPU allocations within one production, including overlapping stages."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .storage import PipelineError


def root():
    path = (
        Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        / "clipforge-gpu-slots"
    )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    (path / "pending-asr").mkdir(mode=0o700, exist_ok=True)
    return path


def pending_path(key, order=0):
    return (
        root()
        / "pending-asr"
        / (f"{order:03d}-" + hashlib.sha256(key.encode()).hexdigest() + ".wait")
    )


def register_pending(key, order=0):
    path = pending_path(key, order)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    return path


def safely_stopped_marker(raw):
    """Only reclaim a stale marker when no assignment could exist or stop was recorded."""
    try:
        owner = Path(json.loads(raw)["control_dir"])
        state_path = owner / "state.json"
        if not state_path.exists():
            # Controller writes state before requesting an assignment.
            return True
        return json.loads(state_path.read_text()).get("status") == "stopped"
    except (OSError, ValueError, KeyError, TypeError):
        return False


class Lease:
    def __init__(self, handle, slot_index):
        self.handle = handle
        self.slot_index = slot_index
        self.confirmed = False
        self.requested = False

    def mark_requested(self):
        self.requested = True

    def confirm_release(self):
        self.confirmed = True


# Colab GPU runtimes one production may hold at once (ASR and render share the pool).
SLOT_COUNT = 5


@contextmanager
def gpu_slot(kind, control_dir, pending=None, timeout=1800):
    """A slot remains reserved after an uncertain stop, even if its process exits."""
    if kind not in {"asr", "render"}:
        raise PipelineError("Unknown GPU slot request")
    directory = root()
    deadline = time.monotonic() + timeout
    handle = None
    while handle is None:
        if time.monotonic() >= deadline:
            raise PipelineError("GPU slot wait timed out; no GPU was requested")
        if kind == "render" and any((directory / "pending-asr").glob("*.wait")):
            time.sleep(0.25)
            continue
        if kind == "asr" and pending is not None:
            waiting = sorted((directory / "pending-asr").glob("*.wait"))
            if waiting and waiting[0] != Path(pending):
                time.sleep(0.25)
                continue
        for index in range(SLOT_COUNT):
            candidate = (directory / ("slot-" + str(index) + ".json")).open("a+")
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                candidate.close()
                continue
            candidate.seek(0)
            marker = candidate.read().strip()
            if marker and not safely_stopped_marker(marker):
                fcntl.flock(candidate, fcntl.LOCK_UN)
                candidate.close()
                continue
            candidate.seek(0)
            candidate.truncate()
            json.dump(
                {"control_dir": str(Path(control_dir).resolve()), "kind": kind},
                candidate,
            )
            candidate.flush()
            os.fsync(candidate.fileno())
            handle = candidate
            if pending is not None:
                Path(pending).unlink(missing_ok=True)
            break
        if handle is None:
            time.sleep(0.25)
    lease = Lease(handle, index)
    try:
        yield lease
    finally:
        if lease.confirmed or not lease.requested:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def clear_stopped(control_dir):
    """After a verified stop, clear only the matching inactive slot marker."""
    expected = str(Path(control_dir).resolve())
    directory = root()
    for index in range(4):
        with (directory / ("slot-" + str(index) + ".json")).open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            handle.seek(0)
            raw = handle.read().strip()
            if raw and json.loads(raw).get("control_dir") == expected:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_UN)
