"""Caption font checks: exact installed family, glyph coverage, and measured widths."""

import shutil
import subprocess
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import ImageFont

from .storage import PipelineError


def resolve(family):
    """Return the installed file for an exact family name; never substitute silently."""
    tool = shutil.which("fc-match")
    if not tool:
        raise PipelineError("fontconfig (fc-match) is required to check caption fonts")
    result = subprocess.run(
        [tool, "-f", "%{file}|%{family}", family],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    path_text, _, families_text = result.stdout.strip().partition("|")
    if result.returncode or not path_text or not families_text:
        raise PipelineError("fontconfig could not resolve the caption font")
    path = Path(path_text)
    families = {name.strip() for name in families_text.split(",")}
    if family not in families or not path.is_file():
        raise PipelineError(
            f"caption font '{family}' is not installed; refusing a silent substitute"
        )
    return path


def missing_glyphs(path, text):
    """Characters the font cannot draw; these would appear as empty boxes.

    Font collections (.ttc, e.g. Noto Sans CJK) use face 0, the same face Pillow measures.
    """
    font = TTFont(str(path), fontNumber=0, lazy=True)
    try:
        cmap = font.getBestCmap() or {}
    finally:
        font.close()
    return sorted({c for c in text if not c.isspace() and ord(c) not in cmap})


class Measure:
    """Pixel widths from the real font file at the caption size."""

    def __init__(self, path, size):
        self.font = ImageFont.truetype(str(path), size)

    def width(self, text):
        return self.font.getlength(text)
