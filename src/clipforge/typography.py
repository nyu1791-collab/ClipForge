"""Measured Japanese line breaks; local BudouX model, no text sent to an API."""

from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata

from .storage import PipelineError, process

NO_START = set(
    "、。，．？！!?：:；;）)]｝}〉》」』】〕ぁぃぅぇぉっゃゅょァィゥェォッャュョー々"
)
NO_END = set("（([｛{〈《「『【〔")


def safe(text):
    # ASS override syntax is never accepted from ASR, titles or user corrections.
    return "".join(
        " " if c in "\r\n\t" else c
        for c in text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
        if unicodedata.category(c) not in {"Cc", "Cf"} or c in "\r\n\t"
    )


@dataclass
class Font:
    family: str
    style: str
    path: str
    index: int
    charset: list[tuple[int, int]]

    def at(self, size):
        from PIL import ImageFont

        return ImageFont.truetype(self.path, size, index=self.index)

    def missing(self, text):
        starts = [a for a, _ in self.charset]
        result = set()
        for char in text:
            if char.isspace():
                continue
            code = ord(char)
            index = bisect_right(starts, code) - 1
            if index < 0 or code > self.charset[index][1]:
                result.add(f"U+{code:04X}")
        return sorted(result)


def resolve_font(name):
    """Fail on a missing requested family instead of silently rendering a fallback."""
    try:
        result = process(
            [
                "fc-match",
                "--format",
                "%{file}\n%{family}\n%{style}\n%{index}\n%{charset}",
                name + ":weight=bold",
            ]
        )
        path, family, style, index, charset = result.stdout.decode().split("\n", 4)
        if name.casefold() not in {f.strip().casefold() for f in family.split(",")}:
            raise PipelineError("requested subtitle font is not installed")
        ranges = []
        for item in charset.split():
            values = item.split("-")
            ranges.append((int(values[0], 16), int(values[-1], 16)))
        return Font(name, style, path, int(index) & 0xFFFF, sorted(ranges))
    except PipelineError:
        raise
    except (OSError, ValueError):
        raise PipelineError("could not resolve subtitle font with fontconfig") from None


@lru_cache(maxsize=1)
def japanese_parser():
    import budoux

    return budoux.load_default_japanese_parser()


def boundaries(text):
    phrases = japanese_parser().parse(text)
    if "".join(phrases) != text:
        raise PipelineError("Japanese segmenter changed caption text")
    result, cursor = {0, len(text)}, 0
    for phrase in phrases:
        cursor += len(phrase)
        result.add(cursor)
    result.update(m.end() for m in re.finditer(r"\s+|[。！？!?]+", text))
    return result


def valid_break(text, index):
    return (
        index == len(text)
        or (text[index] not in NO_START and not unicodedata.combining(text[index]))
    ) and (index == 0 or text[index - 1] not in NO_END)


def wrap_offsets(text, font, limit):
    """Line-end offsets preserving every character, preferring phrase boundaries."""
    ends, cursor, forced = [], 0, False
    preferred = boundaries(text)
    while cursor < len(text):
        low, high = cursor + 1, len(text)
        fits = cursor
        while low <= high:
            mid = (low + high) // 2
            if font.getlength(text[cursor:mid]) <= limit:
                fits, low = mid, mid + 1
            else:
                high = mid - 1
        if fits == cursor:
            raise PipelineError("caption area is narrower than a single glyph")
        if fits < len(text):
            choices = [
                i for i in preferred if cursor < i <= fits and valid_break(text, i)
            ]
            if choices:
                fits = max(choices)
            else:
                allowed = [
                    i for i in range(cursor + 1, fits + 1) if valid_break(text, i)
                ]
                if allowed:
                    fits = max(allowed)
                forced = True
        ends.append(fits)
        cursor = fits
    return ends, forced


def with_breaks(text, offsets, separator=r"\N"):
    pieces, cursor = [], 0
    for end in offsets:
        if cursor >= len(text):
            break
        pieces.append(text[cursor : min(end, len(text))])
        cursor = end
    return separator.join(pieces)
