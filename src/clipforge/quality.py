"""Legacy caption helpers; production quality inspection and enforcement are disabled."""

import math
import re
import unicodedata
from pathlib import Path

import pysubs2

from . import caption_sentences, fonts
from .caption_breaks import MIN_DISPLAY_SECONDS, parser
from .captions import (
    MAX_CAPTION_CPS,
    OUTLINE_ALLOWANCE,
    inside_word,
    scale,
    spoken_length,
)
from .models import CAPTION_LINE_EM, Settings

TEXT_STYLES = {"Default", "Sequential"}
MAX_CAPTION_LINES = 2
READING_CPS_THRESHOLDS = (4, 7, 10)
SHORT_CAPTION_MS = round(MIN_DISPLAY_SECONDS * 1000)  # shorter captions flicker
LONG_CAPTION_MS = 4000  # longer captions outlast the speech they show


def inspect_output(video, has_audio, duration):
    """Legacy entry point: final media rescans and loudness measurements are disabled."""
    return {"integrated_lufs": None, "true_peak_dbtp": None}


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
    text is as tall as its lines; heading styles, which are not bottom-aligned, are
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


def check_clip(directory, rendered, missing_glyphs, settings=None, static=None):
    """Legacy entry point: do not read captions, decode video, or grade delivery."""
    return {
        "status": "not_run",
        "passed": None,
        "verification_scope": "none",
        "full_decode_performed": False,
        "loudness_measured": False,
    }


def record(directory, report):
    """Legacy no-op: do not create a new inspection artifact."""
    return report


def enforce(report, gate):
    """Retain quality findings without blocking delivery, including legacy strict configs."""
    return report
