"""Decode and measure completed files before atomically accepting them."""

import json
import math

from . import media
from .storage import PipelineError, process


def inspect_video(path, duration, width, height):
    info = media.probe(path)
    if (info["width"], info["height"]) != (width, height) or abs(
        info["duration"] - duration
    ) > 0.3:
        raise PipelineError("quality gate: duration or dimensions mismatch")
    if not info["audio"]:
        raise PipelineError("quality gate: missing audio")
    result = process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            *media.PROTOCOLS,
            "-i",
            path,
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout=max(600, duration * 4),
    )
    text = result.stderr.decode(errors="replace")
    try:
        report = json.loads(text[text.rfind("{") : text.rfind("}") + 1])

        def finite(key):
            value = float(report[key])
            return value if math.isfinite(value) else None

        loudness, peak = finite("input_i"), finite("input_tp")
    except (ValueError, KeyError):
        raise PipelineError("quality gate: audio measurement unavailable") from None
    # Decode warnings can indicate truncated/corrupt streams even with exit code zero.
    if any(
        term in text.lower()
        for term in (
            "error while decoding",
            "invalid data found",
            "corrupt decoded frame",
        )
    ):
        raise PipelineError("quality gate: decode errors")
    if peak is not None and peak > -0.5:
        raise PipelineError("quality gate: true peak exceeds -0.5 dBTP")
    warnings = []
    if loudness is None:
        warnings.append("silent_audio")
    elif not -18 <= loudness <= -14:
        warnings.append("loudness_outside_-16_plusminus_2_LUFS")
    return {
        "status": "passed",
        "decoded": True,
        "loudness_lufs": loudness,
        "true_peak_dbtp": peak,
        "warnings": warnings,
        "human_review_performed": False,
    }
