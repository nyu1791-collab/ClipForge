"""Original table, bar and step diagrams (Pillow) and sanitized SVG rasterizing (CairoSVG).

Drawings are rendered at 2x and downsampled for smooth edges. Nothing here loads
external resources: SVG files that reference URLs, images, or other files are refused.
"""

import re
from pathlib import Path

from PIL import Image, ImageDraw

from .fonts import Measure
from .storage import PipelineError

WIDTH = 1000
SUPERSAMPLE = 2
PANEL = (255, 255, 255)
INK = (24, 32, 48)
ACCENT = (255, 196, 0)
MUTED = (110, 120, 140)
ROW_ALT = (244, 246, 250)
# Refuses any reference to another resource, and any DTD (entities can read local files).
EXTERNAL = re.compile(
    r"(href\s*=|xlink|url\s*\(|@import|<!DOCTYPE|<!ENTITY|"
    r"<(?:[\w.-]+:)?(?:image|use|script|foreignObject)\b)",
    re.IGNORECASE,
)


def _fit(text, font_path, size, limit):
    """Text and font for a size in output pixels, drawn on the supersampled canvas.

    The font shrinks to 14 px before the text is cut with an ellipsis, so nothing runs
    past its box. Returns (text, measure).
    """
    size_px, limit_px = size * SUPERSAMPLE, limit * SUPERSAMPLE
    measure = Measure(font_path, size_px)
    while size_px > 14 * SUPERSAMPLE and measure.width(text) > limit_px:
        size_px -= 2 * SUPERSAMPLE
        measure = Measure(font_path, size_px)
    if measure.width(text) > limit_px:
        cut = len(text)
        while cut > 1 and measure.width(text[:cut] + "…") > limit_px:
            cut -= 1
        text = text[:cut] + "…"
    return text, measure


def _canvas(height):
    return Image.new(
        "RGBA", (WIDTH * SUPERSAMPLE, height * SUPERSAMPLE), PANEL + (255,)
    )


def _text(draw, xy, text, measure, fill):
    draw.text(
        (xy[0] * SUPERSAMPLE, xy[1] * SUPERSAMPLE),
        text,
        font=measure.font,
        fill=fill,
    )


def _finish(image, destination):
    final = image.resize((WIDTH, image.height // SUPERSAMPLE), Image.LANCZOS)
    final.convert("RGB").save(destination, format="PNG", optimize=True)
    return destination


def table(title, rows, font_path, destination):
    columns = len(rows[0])
    margin, row_h = 40, 64
    height = 150 + row_h * len(rows)
    image = _canvas(height)
    draw = ImageDraw.Draw(image)
    title, title_m = _fit(title, font_path, 44, WIDTH - 2 * margin)
    _text(draw, (margin, 28), title, title_m, INK)
    cell_w = (WIDTH - 2 * margin) / columns
    top = 110
    for r, row in enumerate(rows):
        y = top + r * row_h
        fill = ACCENT if r == 0 else (ROW_ALT if r % 2 == 0 else PANEL)
        draw.rectangle(
            [
                margin * SUPERSAMPLE,
                y * SUPERSAMPLE,
                (WIDTH - margin) * SUPERSAMPLE,
                (y + row_h) * SUPERSAMPLE,
            ],
            fill=fill,
        )
        for c, cell in enumerate(row):
            cell_text, measure = _fit(str(cell), font_path, 30, cell_w - 24)
            _text(draw, (margin + c * cell_w + 12, y + 16), cell_text, measure, INK)
    return _finish(image, destination)


def bars(title, labels, values, font_path, destination):
    margin, row_h = 40, 74
    height = 150 + row_h * len(labels)
    image = _canvas(height)
    draw = ImageDraw.Draw(image)
    title, title_m = _fit(title, font_path, 44, WIDTH - 2 * margin)
    _text(draw, (margin, 28), title, title_m, INK)
    peak = max(abs(v) for v in values) or 1.0
    label_w = 300
    track = WIDTH - 2 * margin - label_w - 140
    for i, (label, value) in enumerate(zip(labels, values)):
        y = 116 + i * row_h
        label_text, measure = _fit(label, font_path, 30, label_w - 16)
        _text(draw, (margin, y + 14), label_text, measure, INK)
        length = max(4, track * abs(value) / peak)
        x0 = margin + label_w
        draw.rounded_rectangle(
            [
                x0 * SUPERSAMPLE,
                (y + 12) * SUPERSAMPLE,
                (x0 + length) * SUPERSAMPLE,
                (y + 50) * SUPERSAMPLE,
            ],
            radius=12 * SUPERSAMPLE,
            fill=ACCENT,
        )
        number = f"{value:g}"
        number, measure_v = _fit(number, font_path, 30, 120)
        _text(draw, (x0 + length + 14, y + 14), number, measure_v, MUTED)
    return _finish(image, destination)


def steps(title, items, font_path, destination):
    margin, box_h, gap = 40, 96, 26
    height = 150 + (box_h + gap) * len(items)
    image = _canvas(height)
    draw = ImageDraw.Draw(image)
    title, title_m = _fit(title, font_path, 44, WIDTH - 2 * margin)
    _text(draw, (margin, 28), title, title_m, INK)
    for i, item in enumerate(items):
        y = 116 + i * (box_h + gap)
        draw.rounded_rectangle(
            [
                margin * SUPERSAMPLE,
                y * SUPERSAMPLE,
                (WIDTH - margin) * SUPERSAMPLE,
                (y + box_h) * SUPERSAMPLE,
            ],
            radius=16 * SUPERSAMPLE,
            fill=ROW_ALT,
            outline=ACCENT,
            width=3 * SUPERSAMPLE,
        )
        number_text, number_m = _fit(str(i + 1), font_path, 36, 40)
        _text(draw, (margin + 22, y + 26), number_text, number_m, INK)
        item_text, measure = _fit(item, font_path, 32, WIDTH - 2 * margin - 110)
        _text(draw, (margin + 90, y + 28), item_text, measure, INK)
        if i + 1 < len(items):
            cx = (margin + 48) * SUPERSAMPLE
            draw.line(
                [cx, (y + box_h) * SUPERSAMPLE, cx, (y + box_h + gap) * SUPERSAMPLE],
                fill=ACCENT,
                width=4 * SUPERSAMPLE,
            )
    return _finish(image, destination)


def render(diagram, font_path, destination):
    if diagram.kind == "table":
        return table(diagram.title, diagram.rows, font_path, destination)
    if diagram.kind == "bars":
        return bars(
            diagram.title, diagram.labels, diagram.values, font_path, destination
        )
    return steps(diagram.title, diagram.steps, font_path, destination)


def sanitized_svg(path):
    """Return SVG text only if it has no external or file references."""
    text = Path(path).read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise PipelineError("SVG is larger than the allowed size")
    if EXTERNAL.search(text):
        raise PipelineError("SVG references external resources and is refused")
    return text


def svg_to_png(path, destination, width=1280):
    """Rasterize a self-contained SVG with CairoSVG; the input is never modified."""
    try:
        import cairosvg
    except ImportError as error:
        raise PipelineError("cairosvg is required to render SVG visuals") from error
    cairosvg.svg2png(
        bytestring=sanitized_svg(path).encode("utf-8"),
        write_to=str(destination),
        output_width=width,
    )
    return destination
