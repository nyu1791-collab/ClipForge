"""Bounded emotion direction; optional display fonts never gate production."""

from copy import deepcopy
from functools import lru_cache
import math
import subprocess

from . import fonts
from .storage import PipelineError

MOODS = {
    "surprise": (255, 210, 100),
    "joy": (255, 180, 140),
    "frustration": (255, 160, 150),
    "sadness": (165, 195, 220),
    "curiosity": (160, 215, 205),
}
STYLE_VERSION = "emotion-v1"


def annotation(row):
    mood, level = row.get("emotion"), row.get("emotion_level", 0)
    confidence = row.get("emotion_confidence", 0)
    if (
        not isinstance(mood, str)
        or mood not in MOODS
        or type(level) is not int
        or level not in (1, 2)
        or type(confidence) not in (int, float)
        or not math.isfinite(confidence)
        or not 0.8 <= confidence <= 1
    ):
        return {"emotion": "neutral", "emotion_level": 0, "emotion_confidence": 0}
    return {"emotion": mood, "emotion_level": level, "emotion_confidence": confidence}


def for_span(start, end, transcript, origin):
    """Use only an unambiguous annotation covering the whole output token."""
    rows = [
        s
        for s in transcript.segments
        if s.end > start + origin and s.start < end + origin
    ]
    if not rows:
        return "neutral", 0
    values = {(s.emotion, s.emotion_level) for s in rows}
    if len(values) != 1:
        return "neutral", 0
    mood, level = next(iter(values))
    if mood not in MOODS or level not in (1, 2):
        return "neutral", 0
    if any(s.emotion_confidence < 0.8 for s in rows):
        return "neutral", 0
    return mood, level


@lru_cache(maxsize=16)
def optional_serif(base_family):
    try:
        return "Noto Serif CJK JP", fonts.resolve("Noto Serif CJK JP")
    except (PipelineError, OSError, subprocess.SubprocessError):
        return base_family, fonts.resolve(base_family)


def measurements(settings, size):
    """Conservative width for every allowed font and the largest fixed size."""
    family, path = optional_serif(settings.font)
    return family, [
        fonts.AssMeasure(font_path, round(size * 1.08))
        for font_path in (fonts.resolve(settings.font), path)
    ]


def add_styles(subtitles, settings, base_names):
    """Keep speaker identity in the glyph color; put mood in the outline and typography."""
    import pysubs2

    serif, _ = optional_serif(settings.font)
    for name in base_names:
        base = subtitles.styles[name]
        for mood, accent in MOODS.items():
            for level in (1, 2):
                style = deepcopy(base)
                style.fontname = serif if mood == "sadness" else settings.font
                style.fontsize = round(base.fontsize * (1.08 if level == 2 else 1.0))
                style.bold = mood != "sadness"
                # Dark tinted borders keep contrast; known speakers keep their text hue.
                style.outlinecolor = pysubs2.Color(*(round(c * 0.24) for c in accent))
                if name in {"Default", "Sequential"}:
                    style.primarycolor = pysubs2.Color(*accent)
                style.secondarycolor = deepcopy(style.primarycolor)
                if name == "Sequential":
                    style.secondarycolor.a = 255
                subtitles.styles[f"{name}__{mood}_{level}"] = style
