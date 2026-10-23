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

from . import caption_sentences, fonts
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
from .models import CAPTION_LINE_EM, Settings
from .storage import PipelineError, write_json

LOUDNESS_RANGE = (-17.0, -15.0)  # target -16 LUFS, plus or minus 1 LU
MAX_TRUE_PEAK = -1.0  # dBTP; leaves room for lossy encoding overshoot
TEXT_STYLES = {"Default", "Sequential"}
MAX_CAPTION_LINES = 2
READING_CPS_THRESHOLDS = (4, 7, 10)
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
    BudouX phrase edge or a morpheme edge (fugashi) of the whole clip is not a split. Without
    fugashi only the BudouX edges count. The check is a warning: it works from character
    scripts, so a rare legitimate break can be flagged.
    """
    full = "".join(text.replace("\n", "") for _, _, text in events)
    phrase_ends, position = set(), 0
    for phrase in parser().parse(full):
        position += len(phrase)
        phrase_ends.add(position)
    morpheme_ends = _morpheme_ends(full)
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
                if cut not in phrase_ends and cut not in morpheme_ends:
                    found.append(f"{before[-3:]}|{after[:3]}")
    return found


def _morpheme_ends(text):
    """Character positions where a morpheme of the text ends (fugashi); empty without fugashi."""
    try:
        tagger = caption_sentences._tagger()
    except ImportError:
        return set()
    ends, position = set(), 0
    for morpheme in tagger(text):
        start = text.find(morpheme.surface, position)
        if start < 0:
            continue
        position = start + len(morpheme.surface)
        ends.add(position)
    return ends


def _caption_style(subtitles, event):
    """The style an event is drawn with; an unknown name falls back to Default."""
    return subtitles.styles.get(event.style, subtitles.styles["Default"])


def overflowing_lines(path):
    """Caption lines wider than the frame allows, measured again with the caption font.

    The frame width, margins and outline allowance are read from the file, so this checks the
    rendered output rather than the layout that produced it. An event's own left and right
    margins replace the style's when they are set (ASS: 0 means the style's).
    """
    subtitles = pysubs2.load(str(path))
    if not any(event.style in TEXT_STYLES for event in subtitles):
        return []
    frame_width, height = (
        int(subtitles.info["PlayResX"]),
        int(subtitles.info["PlayResY"]),
    )
    measures = {}
    found = []
    for event in subtitles:
        if event.style not in TEXT_STYLES:
            continue
        style = _caption_style(subtitles, event)
        limit = (
            frame_width
            - int(event.marginl or style.marginl)
            - int(event.marginr or style.marginr)
            - round(OUTLINE_ALLOWANCE * scale(height))
        )
        if style.fontname not in measures:
            measures[style.fontname] = fonts.Measure(
                fonts.resolve(style.fontname), int(style.fontsize)
            )
        measure = measures[style.fontname]
        found.extend(
            line for line in event.plaintext.split("\n") if measure.width(line) > limit
        )
    return found


BOTTOM_ALIGNMENTS = {
    pysubs2.Alignment.BOTTOM_LEFT,
    pysubs2.Alignment.BOTTOM_CENTER,
    pysubs2.Alignment.BOTTOM_RIGHT,
}


def outside_frame_captions(path):
    """Bottom-aligned captions whose text would rise above the top edge of the frame.

    Each event's own MarginV sets its bottom edge, or the style's when the event's is 0. The
    text is as tall as its lines; heading and credit styles, which are not bottom-aligned, are
    not checked.
    """
    subtitles = pysubs2.load(str(path))
    if not any(event.style in TEXT_STYLES for event in subtitles):
        return []
    height = int(subtitles.info["PlayResY"])
    found = []
    for event in subtitles:
        if event.style not in TEXT_STYLES:
            continue
        style = _caption_style(subtitles, event)
        if style.alignment not in BOTTOM_ALIGNMENTS:
            continue
        margin = event.marginv or style.marginv
        lines = event.plaintext.split("\n")
        block = len(lines) * style.fontsize * CAPTION_LINE_EM
        if margin + block > height:
            found.append(event.plaintext)
    return found


# Horizontal share of the picture each band region covers.
STATIC_REGIONS = {"whole": (0.0, 1.0), "left": (0.0, 0.5), "right": (0.5, 1.0)}


def static_overlaps(path, static, rendered, settings=None):
    """Captions whose box meets a static band that auto_layout placed them by.

    A caption's box is its MarginV (or the style's) up by its lines times the font height, and
    its width is the widest line measured with the caption font, centred in the picture area
    (the frame less the chat column). static[i] lists caption i's bands as [bottom, top, region],
    shares of the height from the bottom edge, with region "whole", "left" or "right". Returns the
    number of captions that overlap a band, or None when the captions and the bands do not
    correspond one to one.
    """
    subtitles = pysubs2.load(str(path))
    events = [event for event in subtitles if event.style in TEXT_STYLES]
    if len(events) != len(static):
        return None
    height = rendered["height"]
    panel = settings.chat_panel_pixels() if settings is not None else 0
    area = rendered["width"] - panel
    count = 0
    for event, bands in zip(events, static):
        style = _caption_style(subtitles, event)
        lines = event.plaintext.split("\n")
        if settings is not None:
            measure = fonts.Measure(fonts.resolve(settings.font), style.fontsize)
            width = max(measure.width(line) for line in lines)
        else:
            width = area
        bottom = event.marginv or style.marginv
        block = len(lines) * style.fontsize * CAPTION_LINE_EM
        low, high = bottom / height, (bottom + block) / height
        left = max(0.0, 0.5 - width / 2 / area)
        right = min(1.0, 0.5 + width / 2 / area)
        for band_bottom, band_top, region in bands:
            region_left, region_right = STATIC_REGIONS[region]
            if (
                band_bottom < high
                and low < band_top
                and region_left < right
                and left < region_right
            ):
                count += 1
                break
    return count


def loudness_failures(loudness):
    """The delivery bar for audio: a clip outside it is refused, not merely noted."""
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


def japanese_style_warnings(events, landscape, settings=None):
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
        line_chars = (
            settings.caption_line_chars()
            if settings
            else Settings.model_fields["horizontal_line_chars"].default
        )
        long_lines = [
            line
            for text in texts
            for line in text.splitlines()
            if sum(
                1 if unicodedata.east_asian_width(char) in "FWA" else 0.5
                for char in line
            )
            > line_chars
        ]
        if long_lines:
            found.append(
                f"{len(long_lines)} caption line(s) exceed {line_chars} "
                "fullwidth characters, for example " + long_lines[0][:10]
            )
    return found


def _finite(value):
    """JSON has no infinity; silence is reported by a warning instead."""
    return value if value is not None and math.isfinite(value) else None


def check_clip(directory, rendered, missing_glyphs, ledger, settings=None, static=None):
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
    high = outside_frame_captions(directory / "captions.ass")
    if high:
        failures.append(
            f"{len(high)} caption(s) rise above the top of the frame, for example "
            + high[0][:20]
        )
    if static is not None:
        overlapping = static_overlaps(
            directory / "captions.ass", static, rendered, settings
        )
        if overlapping is None:
            warnings.append("captions could not be checked against static elements")
        elif overlapping:
            warnings.append(f"{overlapping} caption(s) overlap static screen elements")
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
        japanese_style_warnings(
            events, rendered["width"] > rendered["height"], settings
        )
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
            failures.extend(loudness_failures(loudness))
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
                str(cps): sum(rate > cps for rate in rates)
                for cps in READING_CPS_THRESHOLDS
            },
            **(
                horizontal_width_metrics(
                    directory / "captions.ass",
                    settings.caption_line_chars()
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
