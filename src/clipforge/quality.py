"""Quality checks on a rendered clip: subtitles, reading speed, loudness and sourcing.

Problems are sorted into failures, which block output in strict mode, and warnings,
which are always reported. The result is written beside the clip as quality.json.
"""

import math
import re
import statistics
import subprocess
from pathlib import Path

import pysubs2

from .captions import MAX_CAPTION_CPS, spoken_length
from .media import AUDIO_RATE
from .storage import PipelineError, write_json

LOUDNESS_RANGE = (-17.0, -15.0)  # target -16 LUFS, plus or minus 1 LU
MAX_TRUE_PEAK = -1.0  # dBTP; leaves room for lossy encoding overshoot
TEXT_STYLES = {"Default", "Sequential"}
MAX_CAPTION_LINES = 2
SHORT_CAPTION_MS = 1000  # shorter captions flicker
LONG_CAPTION_MS = 4000  # longer captions outlast the speech they show


def parse_ebur128_summary(text):
    """Integrated loudness (LUFS) and true peak (dBTP) from an ebur128 summary.

    Both are None when the summary is missing. Silence measures -inf.
    """
    if "Summary" not in text:
        return {"integrated_lufs": None, "true_peak_dbtp": None}
    summary = text[text.rfind("Summary") :]
    integrated = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary)
    peak = re.search(r"True peak:.*?Peak:\s+(-?[\d.]+|-inf) dBFS", summary, re.DOTALL)
    return {
        "integrated_lufs": float(integrated.group(1)) if integrated else None,
        "true_peak_dbtp": float(peak.group(1)) if peak else None,
    }


def measure_output(video):
    """EBU R128 loudness of the rendered audio, measured on the final file."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(Path(video).resolve()),
            "-vn",
            "-af",
            "ebur128=peak=true:framelog=quiet",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    measured = parse_ebur128_summary(proc.stderr)
    if proc.returncode or None in measured.values():
        raise PipelineError("loudness of the rendered clip could not be measured")
    return measured


def decode_errors(video):
    """Messages from decoding the whole file; an empty list means it decodes cleanly."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(Path(video).resolve()),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    lines = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
    if proc.returncode:
        lines.append(f"ffmpeg exited with status {proc.returncode}")
    return lines


def caption_events(path):
    """(start ms, end ms, text) of each spoken caption event in a caption file.

    An empty SRT is a clip without speech; it has no captions rather than an error.
    """
    if not Path(path).read_text(encoding="utf-8").strip():
        return []
    return [
        (event.start, event.end, event.plaintext)
        for event in pysubs2.load(str(path))
        if event.style in TEXT_STYLES
    ]


def caption_rates(path):
    """Reading speed (characters per second) of each spoken caption, and overlap count."""
    rates, overlaps = [], 0
    events = [e for e in pysubs2.load(str(path)) if e.style in TEXT_STYLES]
    previous_end = None
    for event in events:
        if previous_end is not None and event.start < previous_end:
            overlaps += 1
        previous_end = max(previous_end or 0, event.end)
        seconds = (event.end - event.start) / 1000
        spoken = spoken_length(event.plaintext)
        if seconds > 0 and spoken:
            rates.append(spoken / seconds)
    return rates, overlaps


def loudness_warnings(loudness):
    lufs, peak = loudness["integrated_lufs"], loudness["true_peak_dbtp"]
    found = []
    if not math.isfinite(lufs):
        found.append("the rendered audio is silent")
    elif not LOUDNESS_RANGE[0] <= lufs <= LOUDNESS_RANGE[1]:
        found.append(
            f"loudness {lufs:.1f} LUFS is outside "
            f"{LOUDNESS_RANGE[0]:g} to {LOUDNESS_RANGE[1]:g}"
        )
    if math.isfinite(peak) and peak > MAX_TRUE_PEAK:
        found.append(f"true peak {peak:.1f} dBTP is above {MAX_TRUE_PEAK:g}")
    return found


def _finite(value):
    """JSON has no infinity; silence is reported by a warning instead."""
    return value if value is not None and math.isfinite(value) else None


def check_clip(directory, rendered, missing_glyphs, ledger):
    """Quality report for a rendered clip folder; the caller decides what to do with it."""
    failures, warnings = [], []
    events = caption_events(directory / "captions.ass")
    rates, overlaps = caption_rates(directory / "captions.ass")
    if overlaps:
        failures.append(f"{overlaps} caption(s) overlap the previous caption")
    if caption_events(directory / "captions.srt") != events:
        failures.append("captions.srt and captions.ass differ in timing or text")
    if any(text.count("\n") + 1 > MAX_CAPTION_LINES for _, _, text in events):
        failures.append(f"a caption has more than {MAX_CAPTION_LINES} lines")
    if missing_glyphs:
        failures.append("the caption font lacks: " + "".join(missing_glyphs)[:40])
    too_fast = sum(rate > MAX_CAPTION_CPS for rate in rates)
    if too_fast:
        warnings.append(
            f"{too_fast} caption(s) read faster than "
            f"{MAX_CAPTION_CPS:g} characters a second"
        )
    short = sum(end - start < SHORT_CAPTION_MS for start, end, _ in events)
    if short:
        warnings.append(f"{short} caption(s) are shown for less than 1 second")
    long = sum(end - start > LONG_CAPTION_MS for start, end, _ in events)
    if long:
        warnings.append(f"{long} caption(s) are shown for more than 4 seconds")
    decode = decode_errors(directory / "video.mp4")
    if decode:
        failures.append(
            "the rendered video does not decode cleanly: " + decode[0][:160]
        )
    loudness = {"integrated_lufs": None, "true_peak_dbtp": None}
    if rendered["audio"]:
        if rendered["audio_sample_rate"] != AUDIO_RATE:
            failures.append(f"the rendered audio is not {AUDIO_RATE} Hz")
        try:
            loudness = measure_output(directory / "video.mp4")
        except PipelineError:
            failures.append("loudness of the rendered audio could not be measured")
        else:
            warnings.extend(loudness_warnings(loudness))
    for entry in ledger:
        if not entry.get("source_credit") or not entry.get("license"):
            failures.append(f"{entry.get('file')} has no source credit or licence")
    return {
        "passed": not failures,
        "failures": failures,
        "warnings": warnings,
        "metrics": {
            "caption_count": len(rates),
            "caption_max_cps": round(max(rates), 2) if rates else 0.0,
            "caption_median_cps": round(statistics.median(rates), 2) if rates else 0.0,
            "captions_over_limit": too_fast,
            "captions_under_1s": short,
            "captions_over_4s": long,
            "integrated_lufs": _finite(loudness["integrated_lufs"]),
            "true_peak_dbtp": _finite(loudness["true_peak_dbtp"]),
            "duration": rendered["duration"],
            "width": rendered["width"],
            "height": rendered["height"],
        },
    }


def record(directory, report):
    write_json(directory / "quality.json", report)
    return report


def enforce(report, gate):
    """Strict mode refuses a clip with failures; report mode only records them."""
    if gate == "strict" and not report["passed"]:
        raise PipelineError(
            "clip failed the quality gate: " + "; ".join(report["failures"])
        )
    return report
