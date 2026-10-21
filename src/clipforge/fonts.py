"""Resolve the actual bold caption font, measure it, and check glyph coverage."""

import subprocess
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import ImageFont

from .storage import PipelineError


def bundled_font(family, directory):
    choices = []
    for path in Path(directory).iterdir():
        if path.suffix.lower() not in {".ttf", ".otf", ".ttc"} or path.is_symlink():
            continue
        if path.suffix.lower() == ".ttc":
            from fontTools.ttLib import TTCollection

            collection = TTCollection(str(path), lazy=True)
            count = len(collection.fonts)
            collection.close()
        else:
            count = 1
        for index in range(count):
            font = TTFont(str(path), fontNumber=index, lazy=True)
            try:
                names = {
                    n.toUnicode() for n in font["name"].names if n.nameID in {1, 16}
                }
                if family in names:
                    bold = (
                        bool(font["OS/2"].fsSelection & 32) if "OS/2" in font else False
                    )
                    choices.append((bold, str(path), index))
            finally:
                font.close()
    if not choices:
        raise PipelineError("selected font family is absent from fonts_dir")
    _, path, index = max(choices)
    return path, index


class Measure:
    def __init__(self, family, size, directory=None):
        if directory:
            path, index = bundled_font(family, directory)
            self.path, self.index = Path(path), index
            self.font = ImageFont.truetype(path, size, index=index)
            return
        result = subprocess.run(
            ["fc-match", "-f", "%{file}|%{family}|%{index}", family + ":style=Bold"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        path, names, index = result.stdout.strip().split("|")
        if (
            family not in {n.strip() for n in names.split(",")}
            or not Path(path).is_file()
        ):
            raise PipelineError(
                "caption font is not installed; install the selected family"
            )
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
