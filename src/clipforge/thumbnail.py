"""1280x720 thumbnail: a real frame from the rendered clip with a short title drawn over it."""

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .fonts import Measure
from .storage import PipelineError

SIZE = (1280, 720)
MARGIN = 60


def make(video, destination, title, font_path, at_seconds):
    frame_path = Path(destination).with_suffix(".frame.png")
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-n",
            "-ss",
            f"{max(0.0, at_seconds):.3f}",
            "-i",
            str(Path(video).resolve()),
            "-frames:v",
            "1",
            str(frame_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode or not frame_path.is_file():
        raise PipelineError("thumbnail frame could not be extracted")
    image = Image.open(frame_path).convert("RGB")
    # Cover-fit to 16:9, then darken the lower part so the title reads clearly.
    scale = max(SIZE[0] / image.width, SIZE[1] / image.height)
    image = image.resize((round(image.width * scale), round(image.height * scale)))
    left, top = (image.width - SIZE[0]) // 2, (image.height - SIZE[1]) // 2
    image = image.crop((left, top, left + SIZE[0], top + SIZE[1]))
    shade = Image.new("L", SIZE, 0)
    ImageDraw.Draw(shade).rectangle([0, SIZE[1] // 3, SIZE[0], SIZE[1]], fill=170)
    shade = shade.filter(ImageFilter.GaussianBlur(40))
    image = Image.composite(Image.new("RGB", SIZE, (0, 0, 0)), image, shade)
    draw = ImageDraw.Draw(image)
    size = 96
    while size > 40:
        measure = Measure(font_path, size)
        if measure.width(title) <= SIZE[0] - 2 * MARGIN:
            break
        size -= 4
    measure = Measure(font_path, size)
    width = measure.width(title)
    x = (SIZE[0] - width) / 2
    y = SIZE[1] - MARGIN - size
    draw.text(
        (x, y),
        title,
        font=measure.font,
        fill=(255, 196, 0),
        stroke_width=max(4, size // 14),
        stroke_fill=(0, 0, 0),
    )
    image.save(destination, format="PNG", optimize=True)
    frame_path.unlink(missing_ok=True)
    return destination
