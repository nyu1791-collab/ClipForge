"""Resolve the actual bold caption font, measure it, and check glyph coverage."""

import subprocess
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import ImageFont

from .storage import PipelineError


class Measure:
    def __init__(self, family, size):
        result = subprocess.run(
            ["fc-match", "-f", "%{file}|%{family}|%{index}", family + ":style=Bold"],
            capture_output=True, text=True, timeout=20, check=True,
        )
        path, names, index = result.stdout.strip().split("|")
        if family not in {n.strip() for n in names.split(",")} or not Path(path).is_file():
            raise PipelineError("caption font is not installed; install the selected family")
        self.path, self.index = Path(path), int(index) & 0xFFFF
        self.font = ImageFont.truetype(path, size, index=self.index)

    def width(self, text):
        return self.font.getlength(text)

    def missing(self, text):
        font = TTFont(str(self.path), fontNumber=self.index, lazy=True)
        try:
            cmap = font.getBestCmap() or {}
            return sorted({c for c in text if not c.isspace() and ord(c) not in cmap})
        finally:
            font.close()
