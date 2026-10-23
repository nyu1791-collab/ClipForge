"""Verify audio artifacts against caller-supplied provenance before reuse."""

import json
import math
from pathlib import Path
import re

from .media_sources import canonical
from .storage import PipelineError, digest


MANIFEST_FIELDS = {
    "schema",
    "request_id",
    "source_ref",
    "workflow_run",
    "source_url",
    "audio_sha256",
    "audio_bytes",
    "audio_info",
    "source_has_video",
    "asr_started",
}


def verify_audio_artifact(
    directory,
    *,
    source_url,
    source_ref,
    request_id,
    workflow_run,
    max_bytes,
    max_seconds,
):
    root = Path(directory)
    manifest, audio = root / "audio-source.json", root / "audio.m4a"
    if root.is_symlink() or any(
        path.is_symlink() or not path.is_file() for path in (manifest, audio)
    ):
        raise PipelineError("verified audio artifact must contain regular files")
    if not 0 < manifest.stat().st_size <= 16384:
        raise PipelineError("invalid audio artifact manifest size")
    canonical(source_url)
    try:
        value = json.loads(manifest.read_text())
        if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
            raise ValueError
        if (
            not re.fullmatch(r"[a-f0-9]{40}", source_ref)
            or not re.fullmatch(r"[a-f0-9]{32}", request_id)
            or type(workflow_run) is not int
            or workflow_run < 1
        ):
            raise ValueError
        expected = {
            "schema": 1,
            "source_url": source_url,
            "source_ref": source_ref,
            "request_id": request_id,
            "workflow_run": workflow_run,
            "asr_started": False,
        }
        if (
            any(value.get(key) != item for key, item in expected.items())
            or type(value["schema"]) is not int
            or type(value["workflow_run"]) is not int
            or type(value["asr_started"]) is not bool
        ):
            raise ValueError
        size, info = value["audio_bytes"], value["audio_info"]
        if (
            type(size) is not int
            or not 0 < size <= min(max_bytes, 2 * 1024**3)
            or audio.stat().st_size != size
        ):
            raise ValueError
        if type(value["source_has_video"]) is not bool or not re.fullmatch(
            r"[a-f0-9]{64}", value["audio_sha256"]
        ):
            raise ValueError
        if (
            not isinstance(info, dict)
            or info.get("audio") is not True
            or type(info.get("audio_start")) not in (int, float)
            or info.get("audio_start") != 0
            or type(info.get("duration")) not in (int, float)
            or not math.isfinite(info["duration"])
            or not 0 < info["duration"] <= max_seconds
        ):
            raise ValueError
        if digest(audio) != value["audio_sha256"]:
            raise ValueError
    except (ValueError, KeyError, TypeError, OSError):
        raise PipelineError(
            "audio artifact identity, hash, duration or size mismatch"
        ) from None
    return value
