"""Quality checks on a rendered clip: subtitles, reading speed, loudness and sourcing.

Problems are sorted into failures, which block output in strict mode, and warnings,
which are always reported. The result is written beside the clip as quality.json.
"""

import math
import re
import statistics
import subprocess
import unicodedata
from pathlib import Path

import pysubs2

from . import fonts
from .caption_breaks import MIN_DISPLAY_SECONDS, parser
from .captions import (
    MAX_CAPTION_CPS,
    OUTLINE_ALLOWANCE,
    inside_word,
    orphan_start,
    scale,
    spoken_length,
)
from .media import AUDIO_RATE
from .models import Settings
from .storage import PipelineError, write_json

LOUDNESS_RANGE = (-17.0, -15.0)  # target -16 LUFS, plus or minus 1 LU
MAX_TRUE_PEAK = -1.0  # dBTP; leaves room for lossy encoding overshoot
TEXT_STYLES = {"Default", "Sequential"}
MAX_CAPTION_LINES = 2
MAX_JAPANESE_LINE_CHARS = 13
SHORT_CAPTION_MS = round(MIN_DISPLAY_SECONDS * 1000)  # shorter captions flicker
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


def word_splits(events):
    """Places where a line or a caption starts inside a word, shown as text like 'ぐ奪|われ'.

    Checked between the lines of each caption and between consecutive captions. A break on a
    BudouX phrase edge of the whole clip is not a split. The check is a warning: it works from
    character scripts, so a rare legitimate break can be flagged.
    """
    full = "".join(text.replace("\n", "") for _, _, text in events)
    phrase_ends, position = set(), 0
    for phrase in parser().parse(full):
        position += len(phrase)
        phrase_ends.add(position)
    found, offset = [], 0
    for index, (_, _, text) in enumerate(events):
        lines = text.split("\n")
        # Each cut is (text before it, text after it, absolute position in the clip text).
        cuts, position = [], offset
        for before, after in zip(lines, lines[1:]):
            position += len(before)
            cuts.append((before, after, position))
        offset += len(text.replace("\n", ""))
        if index + 1 < len(events):
            following = events[index + 1][2].split("\n")[0]
            cuts.append((lines[-1], following, offset))
        for before, after, cut in cuts:
            if before and after and inside_word(before[-1], after[0]):
                if cut not in phrase_ends:
                    found.append(f"{before[-3:]}|{after[:3]}")
    return found


def overflowing_lines(path):
    """Caption lines wider than the frame allows, measured again with the caption font.

    The frame width, margins and outline allowance are read from the file, so this checks the
    rendered output rather than the layout that produced it.
    """
    subtitles = pysubs2.load(str(path))
    if not any(event.style in TEXT_STYLES for event in subtitles):
        return []
    style = subtitles.styles["Default"]
    frame_width, height = (
        int(subtitles.info["PlayResX"]),
        int(subtitles.info["PlayResY"]),
    )
    limit = (
        frame_width
        - int(style.marginl)
        - int(style.marginr)
        - round(OUTLINE_ALLOWANCE * scale(height))
    )
    measure = fonts.Measure(fonts.resolve(style.fontname), int(style.fontsize))
    return [
        line
        for event in subtitles
        if event.style in TEXT_STYLES
        for line in event.plaintext.split("\n")
        if measure.width(line) > limit
    ]


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


def reading_limit(rendered, settings=None):
    """Characters a second a caption may read at: stricter for landscape frames."""
    landscape = rendered["width"] > rendered["height"]
    if landscape:
        if settings:
            return settings.horizontal_warn_cps
        return Settings.model_fields["horizontal_warn_cps"].default
    return MAX_CAPTION_CPS


def horizontal_width_metrics(path, line_chars):
    """Measure the horizontal layout limit independently; never return caption text."""
    subtitles = pysubs2.load(str(path))
    measures, equivalents, over = {}, [], 0
    for event in subtitles:
        if event.style not in TEXT_STYLES:
            continue
        style = subtitles.styles[event.style]
        key = (style.fontname, int(style.fontsize))
        if key not in measures:
            measures[key] = fonts.Measure(fonts.resolve(key[0]), key[1])
        measure = measures[key]
        full_width = measure.width("あ")
        limit = round(measure.width("あ" * line_chars))
        for line in event.plaintext.splitlines():
            width = measure.width(line)
            equivalents.append(width / full_width if full_width else 0)
            over += width > limit
    return {
        "horizontal_line_chars": line_chars,
        "line_max_fullwidth_equivalents": round(max(equivalents, default=0), 3),
        "lines_over_horizontal_limit": over,
    }


def japanese_style_warnings(events, landscape):
    """Netflix Japanese style checks; character counts are independent of font pixels."""
    texts = [text for _, _, text in events]
    found = []
    punctuation = sum(any(mark in text for mark in "。、") for text in texts)
    if punctuation:
        found.append(f"{punctuation} caption(s) contain Japanese full stops or commas")
    halfwidth = [text for text in texts if re.search(r"[?!]", text)]
    if halfwidth:
        found.append(
            f"{len(halfwidth)} caption(s) use halfwidth question or exclamation marks, "
            "use fullwidth marks, for example " + halfwidth[0].replace("\n", " ")[:10]
        )
    unspaced = [
        text
        for text in texts
        if re.search(r"[?!？！][ \t]*[^\s?!？！」』）)\]］】〉》、。…‥]", text)
    ]
    if unspaced:
        found.append(
            f"{len(unspaced)} caption(s) lack a fullwidth space after a question or "
            "exclamation mark before more text, for example "
            + unspaced[0].replace("\n", " ")[:10]
        )
    if landscape:
        long_lines = [
            line
            for text in texts
            for line in text.splitlines()
            if sum(
                1 if unicodedata.east_asian_width(char) in "FWA" else 0.5
                for char in line
            )
            > MAX_JAPANESE_LINE_CHARS
        ]
        if long_lines:
            found.append(
                f"{len(long_lines)} caption line(s) exceed {MAX_JAPANESE_LINE_CHARS} "
                "fullwidth characters, for example " + long_lines[0][:10]
            )
    return found


def _finite(value):
    """JSON has no infinity; silence is reported by a warning instead."""
    return value if value is not None and math.isfinite(value) else None


def check_clip(directory, rendered, missing_glyphs, ledger, settings=None):
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
    limit = reading_limit(rendered, settings)
    too_fast = sum(rate > limit for rate in rates)
    if too_fast:
        warnings.append(
            f"{too_fast} caption(s) read faster than {limit:g} characters a second"
        )
    short = sum(end - start < SHORT_CAPTION_MS for start, end, _ in events)
    if short:
        warnings.append(
            f"{short} caption(s) are shown for less than {MIN_DISPLAY_SECONDS:g} second"
        )
    long = sum(end - start > LONG_CAPTION_MS for start, end, _ in events)
    if long:
        warnings.append(f"{long} caption(s) are shown for more than 4 seconds")
    overflow = overflowing_lines(directory / "captions.ass")
    if overflow:
        failures.append(
            f"{len(overflow)} caption line(s) are wider than the frame, for example "
            + overflow[0][:20]
        )
    starts = [orphan_start(text) for _, _, text in events]
    if "punctuation" in starts:
        failures.append("a caption starts with a punctuation mark")
    orphans = starts.count("particle")
    if orphans:
        warnings.append(
            f"{orphans} caption(s) start with a sentence-final particle, a stray word"
        )
    splits = word_splits(events)
    if splits:
        warnings.append(
            f"{len(splits)} line or caption break(s) fall inside a word, for example "
            + splits[0]
        )
    warnings.extend(
        japanese_style_warnings(events, rendered["width"] > rendered["height"])
    )
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
            "word_splits": len(splits),
            "orphan_particles": orphans,
            "reading_limit_cps": limit,
            "reading_target_cps": (
                settings.horizontal_target_cps
                if settings
                else Settings.model_fields["horizontal_target_cps"].default
            )
            if rendered["width"] > rendered["height"]
            else 10,
            "reading_count_method": "non_whitespace_codepoints",
            "captions_over_cps": {
                str(cps): sum(rate > cps for rate in rates) for cps in (4, 7, 10)
            },
            **(
                horizontal_width_metrics(
                    directory / "captions.ass",
                    settings.horizontal_line_chars
                    if settings
                    else Settings.model_fields["horizontal_line_chars"].default,
                )
                if rendered["width"] > rendered["height"]
                else {}
            ),
            "line_overflows": len(overflow),
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
