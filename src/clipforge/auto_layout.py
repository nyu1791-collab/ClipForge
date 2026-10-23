"""Choose the crop side and caption band for the landscape chat layout from frame content.

The 16:9 source keeps `keep` of its width (the left 1440x1080 of a 1920x1080 frame). The
crop side is the window that keeps the most detail (gradient energy). A caption moves to
the top band only when the bottom band is busy and the caption is not a quiet moment.
"""

import time

from . import storage
from .storage import PipelineError


ANCHORS = ("left", "center", "right")
# The center window is kept unless a side window keeps at least this share more of the energy.
CENTER_MARGIN = 0.02
# Captions this close (seconds) in sequence share one band, so the band does not flicker.
GROUP_GAP = 2.0
# Frames are processed in blocks so float32 temporaries stay small for long samples.
BLOCK = 256


def _window(width, x, keep):
    """Column range [left, right) of a window that starts at fraction x and keeps `keep`."""
    left = round(x * width)
    return left, min(width, left + round(keep * width))


def _blocks(frames):
    for index in range(0, len(frames), BLOCK):
        yield frames[index : index + BLOCK]


def _gradient(block):
    """Sobel-style gradient magnitude |gx| + |gy| per pixel, shape [k, h, w], float32."""
    import numpy as np

    padded = np.pad(block.astype(np.float32), ((0, 0), (1, 1), (1, 1)), mode="edge")
    gx = (padded[:, :-2, 2:] + 2 * padded[:, 1:-1, 2:] + padded[:, 2:, 2:]) - (
        padded[:, :-2, :-2] + 2 * padded[:, 1:-1, :-2] + padded[:, 2:, :-2]
    )
    gy = (padded[:, 2:, :-2] + 2 * padded[:, 2:, 1:-1] + padded[:, 2:, 2:]) - (
        padded[:, :-2, :-2] + 2 * padded[:, :-2, 1:-1] + padded[:, :-2, 2:]
    )
    return np.abs(gx) + np.abs(gy)


def _check_frames(frames, message):
    if frames.ndim != 3 or frames.shape[0] == 0:
        raise PipelineError(message)


def sample_frames(source, start, end, *, fps=2.0, width=320, height=180):
    """Grayscale frames of [start, end) as (times in source seconds, uint8 [n, height, width])."""
    import numpy as np

    if not end > start:
        raise ValueError("sample window must end after it starts")
    if fps <= 0 or width <= 0 or height <= 0:
        raise ValueError("fps, width and height must be positive")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{end - start:.3f}",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-vf",
        f"fps={fps:g},scale={width}:{height}",
        "-pix_fmt",
        "gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    # Decoding time grows with the window; a fixed limit would fail long topics.
    timeout = max(120, int((end - start) * 2) + 60)
    result = storage.process(command, timeout=timeout, stage="layout-frames")
    size = width * height
    count = len(result.stdout) // size
    if count == 0:
        raise PipelineError("no video frames in the layout sample window")
    frames = np.frombuffer(result.stdout, dtype=np.uint8, count=count * size)
    frames = frames.reshape(count, height, width)
    times = [start + index / fps for index in range(count)]
    return times, frames


def choose_crop(frames, *, keep=0.75):
    """Pick the anchor of the kept-width window that holds the most gradient energy."""
    import numpy as np

    if not 0 < keep <= 1:
        raise ValueError("keep must be in (0, 1]")
    frames = np.asarray(frames)
    _check_frames(frames, "no frames to choose a crop from")
    count, _, width = frames.shape
    profile = np.zeros(width, dtype=np.float64)
    for block in _blocks(frames):
        # Sum rows (per column) and frames; divided by the frame count below.
        profile += _gradient(block).sum(axis=(0, 1))
    profile /= count
    total = float(profile.sum())

    fractions = {"left": 0.0, "center": (1 - keep) / 2, "right": 1 - keep}
    kept = {}
    for anchor in ANCHORS:
        left, right = _window(width, fractions[anchor], keep)
        # A flat sample has no energy to lose; treat every window as keeping its width.
        kept[anchor] = float(profile[left:right].sum()) / total if total > 0 else keep

    best = max(ANCHORS, key=lambda anchor: kept[anchor])
    if kept[best] - kept["center"] < CENTER_MARGIN:
        best = "center"
    return {
        "anchor": best,
        "x": fractions[best],
        "keep": keep,
        "kept": {anchor: round(kept[anchor], 4) for anchor in ANCHORS},
    }


def _band_densities(frames, left, right, rows):
    """Mean gradient in the top and bottom bands of the window, per frame: (bottom, top)."""
    import numpy as np

    height = frames.shape[1]
    bottom, top = [], []
    for block in _blocks(frames):
        region = _gradient(block)[:, :, left:right]
        bottom.append(region[:, height - rows :, :].mean(axis=(1, 2)))
        top.append(region[:, :rows, :].mean(axis=(1, 2)))
    return np.concatenate(bottom), np.concatenate(top)


def _frames_for(times, start, end):
    """Indices of the frames inside [start, end]; if none, the nearest frame(s) in time."""
    distances = [max(start - moment, 0.0, moment - end) for moment in times]
    nearest = min(distances)
    return [index for index, distance in enumerate(distances) if distance == nearest]


def _steady(events, raw):
    """Captions less than GROUP_GAP apart in sequence take the majority band of their group."""
    order = sorted(range(len(events)), key=lambda index: events[index])
    groups, current, reach = [], [], 0.0
    for index in order:
        start, end = events[index]
        if current and start - reach >= GROUP_GAP:
            groups.append(current)
            current = []
        reach = max(reach, end) if current else end
        current.append(index)
    if current:
        groups.append(current)

    result = list(raw)
    for group in groups:
        tops = sum(1 for index in group if raw[index] == "top")
        # A tie keeps the default band.
        majority = "top" if tops * 2 > len(group) else "bottom"
        for index in group:
            result[index] = majority
    return result


def caption_positions(times, frames, events, crop, *, band=0.24, ratio=1.4, floor=None):
    """Band ("top" or "bottom") per caption event, from the detail in the crop window's bands."""
    import numpy as np

    frames = np.asarray(frames)
    _check_frames(frames, "no frames to place captions on")
    if len(times) != frames.shape[0]:
        raise ValueError("times and frames must have the same length")
    times = [float(moment) for moment in times]
    _, height, width = frames.shape
    left, right = _window(width, crop["x"], crop["keep"])
    rows = max(1, round(band * height))
    bottom, top = _band_densities(frames, left, right, rows)
    if floor is None:
        floor = float(bottom.mean())

    raw = []
    for start, end in events:
        index = _frames_for(times, start, end)
        bottom_density = float(bottom[index].mean())
        top_density = float(top[index].mean())
        busy = bottom_density > ratio * top_density and bottom_density > floor
        raw.append("top" if busy else "bottom")
    return _steady([(float(start), float(end)) for start, end in events], raw)


def plan(source, start, end, events, *, keep=0.75):
    """Crop side and caption bands for [start, end) of `source`; events are (start, end) seconds."""
    began = time.monotonic()
    times, frames = sample_frames(source, start, end)
    crop = choose_crop(frames, keep=keep)
    positions = caption_positions(times, frames, events, crop)
    return {
        "crop": crop,
        "positions": positions,
        "frames": len(times),
        "seconds": round(time.monotonic() - began, 3),
    }
