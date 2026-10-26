"""A few reference frames for manual corrections; never a delivery quality gate."""

import io
from pathlib import Path
import subprocess


def make_sheet(directory, samples, duration):
    from PIL import Image, ImageDraw, ImageOps

    directory = Path(directory)
    chosen, seen = [], set()
    for sample in samples:
        at = max(0, min(float(sample["at"]), max(0, duration - 0.05)))
        if round(at, 2) not in seen:
            chosen.append({**sample, "at": at})
            seen.add(round(at, 2))
        if len(chosen) == 3:
            break
    if not chosen:
        return {"status": "not_created_no_caption_samples"}
    tiles, applied = [], []
    for sample in chosen:
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    str(sample["at"]),
                    "-i",
                    str(directory / "video.mp4"),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=960:-2",
                    "-c:v",
                    "mjpeg",
                    "-f",
                    "image2pipe",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode:
                continue
            with Image.open(io.BytesIO(result.stdout)) as frame:
                tile = ImageOps.pad(frame.convert("RGB"), (960, 540), color="black")
            label = Image.new("RGB", (960, 566), "black")
            label.paste(tile, (0, 26))
            ImageDraw.Draw(label).text(
                (10, 6), f"{sample['at']:.2f}s  {sample['style']}", fill="white"
            )
            tiles.append(label)
            applied.append(sample)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    if not tiles:
        return {"status": "not_created_frame_unavailable"}
    sheet = Image.new(
        "RGB", (960 * min(2, len(tiles)), 566 * ((len(tiles) + 1) // 2)), "black"
    )
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % 2) * 960, (index // 2) * 566))
    sheet.save(directory / "contact-sheet.jpg", quality=92)
    return {
        "status": "created_reference_only",
        "file": "contact-sheet.jpg",
        "samples": applied,
        "automated_quality_assessment": "not_run",
    }
