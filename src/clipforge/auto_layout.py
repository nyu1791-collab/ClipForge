"""Choose the crop side and caption band for the landscape chat layout from frame content.

The 16:9 source keeps `keep` of its width (the left 1440x1080 of a 1920x1080 frame). The
crop side is the window that keeps the most detail (gradient energy). Captions stay at the
bottom; a caption is lifted just above a busy band (HUD, hotbar, overlay) that touches the
bottom edge, up to MAX_LIFT of the height.
"""

import time

from . import storage
from .storage import PipelineError


ANCHORS = ("left", "center", "right")
# The center window is kept unless the best window keeps at least this share more of the energy.
CENTER_MARGIN = 0.02
# Pixels below this value count as dark (a black chat panel, a night scene).
DARK_LEVEL = 32
# A column whose share of dark pixels reaches this is background: its energy counts as 0.
DARK_SHARE = 0.45
# A dark run at a frame edge is an overlay only up to this share of the width (a night scene is not).
OVERLAY_MAX_SHARE = 0.40
# Candidate crop offsets are evaluated in steps of this share of the source width.
OFFSET_STEP = 1 / 32
# Captions this close (seconds) in sequence share one lift, so the caption does not flicker.
GROUP_GAP = 2.0
# A row is busy when its density is above this multiple of the window's median row density.
BUSY_RATIO = 1.6
# Busy rows are searched from the bottom edge up to this share of the height.
SEARCH = 0.40
# Quiet rows inside a busy band are tolerated up to this share of the height.
BAND_GAP = 0.015
# A caption's bottom edge sits this share of the height above the top of the busy band.
LIFT_PAD = 0.015
# No caption is lifted above this share of the height.
MAX_LIFT = 0.32
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


def _overlay_block(dark, width):
    """The dark run touching the left or right edge that may be ignored, or None.

    Returns (side, start_px, end_px). The run must be contiguous from the edge and at most
    OVERLAY_MAX_SHARE of the width, so a night scene or a dark spot in the middle is kept.
    """
    left = 0
    while left < width and dark[left]:
        left += 1
    right = width
    while right > 0 and dark[right - 1]:
        right -= 1
    blocks = []
    if 0 < left and left / width <= OVERLAY_MAX_SHARE:
        blocks.append(("left", 0, left))
    if 0 < width - right and (width - right) / width <= OVERLAY_MAX_SHARE:
        blocks.append(("right", right, width))
    if not blocks:
        return None
    return max(blocks, key=lambda block: block[2] - block[1])


def choose_crop(frames, *, keep=0.75):
    """Pick the crop offset whose kept-width window holds the most detail.

    Detail is gradient energy per column. An overlay (a dark block at the left or right
    edge, such as a black chat panel with its text) counts as 0 and the window is moved
    clear of it when the window's range allows. The window is searched on a grid of
    OFFSET_STEP from 0 to (1 - keep). A best window within CENTER_MARGIN of the center
    gives "center". Ties go to the offset nearest the center.
    """
    import numpy as np

    if not 0 < keep <= 1:
        raise ValueError("keep must be in (0, 1]")
    frames = np.asarray(frames)
    _check_frames(frames, "no frames to choose a crop from")
    count, height, width = frames.shape
    profile = np.zeros(width, dtype=np.float64)
    dark = np.zeros(width, dtype=np.float64)
    for block in _blocks(frames):
        # Sum rows (per column) and frames; divided by the frame count below.
        profile += _gradient(block).sum(axis=(0, 1))
        dark += (block < DARK_LEVEL).sum(axis=(0, 1))
    profile /= count
    dark /= count * height
    raw_total = float(profile.sum())

    overlay = _overlay_block(dark >= DARK_SHARE, width)
    corrected = profile.copy()
    if overlay is not None:
        _, start_px, end_px = overlay
        corrected[start_px:end_px] = 0.0
    total = float(corrected.sum())

    slack = 1 - keep
    center_x = slack / 2

    def share(values, whole, x):
        left, right = _window(width, x, keep)
        # A flat sample has no energy to lose; treat every window as keeping its width.
        return float(values[left:right].sum()) / whole if whole > 0 else keep

    steps = [
        index * OFFSET_STEP for index in range(int(slack / OFFSET_STEP + 1e-9) + 1)
    ]
    candidates = sorted({round(x, 9) for x in steps + [slack, center_x]})
    scored = [(x, share(corrected, total, x)) for x in candidates]
    peak = max(value for _, value in scored)
    if peak - share(corrected, total, center_x) < CENTER_MARGIN:
        x = center_x
    else:
        ties = [x for x, value in scored if value >= peak - 1e-12]
        x = min(ties, key=lambda candidate: abs(candidate - center_x))

    # Move the window clear of the overlay when the window's range allows it.
    overlay_info = {"side": None, "start": 0.0, "end": 0.0}
    if overlay is not None:
        side, start_px, end_px = overlay
        overlay_info = {
            "side": side,
            "start": round(start_px / width, 4),
            "end": round(end_px / width, 4),
        }
        win = round(keep * width)
        left_px = round(x * width)
        if side == "right" and left_px + win > start_px:
            target = start_px - win
            if 0 <= target <= width - win:
                x = target / width
        elif side == "left" and left_px < end_px:
            target = end_px
            if target + win <= width:
                x = target / width

    if abs(x - center_x) < 1e-9:
        anchor = "center"
    elif abs(x) < 1e-9:
        anchor = "left"
    elif abs(x - slack) < 1e-9:
        anchor = "right"
    else:
        anchor = "offset"

    anchor_x = {"left": 0.0, "center": center_x, "right": slack}
    return {
        "anchor": anchor,
        "x": x,
        "keep": keep,
        "kept": {a: round(share(corrected, total, anchor_x[a]), 4) for a in ANCHORS},
        "raw_kept": {
            a: round(share(profile, raw_total, anchor_x[a]), 4) for a in ANCHORS
        },
        "overlay": overlay_info,
    }


def _frames_for(times, start, end):
    """Indices of the frames inside [start, end]; if none, the nearest frame(s) in time."""
    distances = [max(start - moment, 0.0, moment - end) for moment in times]
    nearest = min(distances)
    return [index for index, distance in enumerate(distances) if distance == nearest]


def _groups(events):
    """Runs of caption indices: a caption starting less than GROUP_GAP after the last run ends joins it."""
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
    return groups


def _shared_max(events, values):
    """Each caption takes the largest value of its run, so captions in one run do not flicker."""
    result = list(values)
    for group in _groups(events):
        peak = max(values[index] for index in group)
        for index in group:
            result[index] = peak
    return result


def _window_columns(crop, width, left, right):
    """Mask of the window's columns that are not under the chat overlay (a dark block at an edge)."""
    import numpy as np

    columns = np.ones(right - left, dtype=bool)
    overlay = crop.get("overlay") or {}
    if overlay.get("side") is not None:
        start = round(float(overlay["start"]) * width)
        end = round(float(overlay["end"]) * width)
        lo, hi = max(start, left) - left, min(end, right) - left
        if hi > lo:
            columns[lo:hi] = False
    return columns


def _row_density(frames, left, right, columns):
    """Mean gradient of each row over the window's columns, per frame: shape [frames, height]."""
    import numpy as np

    rows = []
    for block in _blocks(frames):
        region = _gradient(block)[:, :, left:right][:, :, columns]
        rows.append(region.mean(axis=2))
    return np.concatenate(rows)


def _band_top(busy, search, tolerance):
    """Top row of the busy band that touches the bottom edge, or None.

    Walks up from the bottom row through the bottom `search` share of the height. Quiet rows
    up to `tolerance` rows in a row do not end the band; a quiet stretch longer than that does.
    """
    height = len(busy)
    limit = height - round(search * height)
    top, quiet = None, 0
    for row in range(height - 1, limit - 1, -1):
        if busy[row]:
            top, quiet = row, 0
        else:
            quiet += 1
            if quiet > tolerance:
                break
    return top


def caption_lifts(
    times,
    frames,
    events,
    crop,
    *,
    search=SEARCH,
    max_lift=MAX_LIFT,
    base=None,
    pad=LIFT_PAD,
):
    """Bottom margin of each caption, as a share of the height ("lift"), from the busy rows.

    A row is busy when its gradient density, in the caption's frames, is above BUSY_RATIO times
    the median row density of the whole window. A caption sits at the base margin unless a busy
    band touches the bottom edge (searched up to `search` of the height) and the band's top is
    above the base; then the caption's bottom edge sits `pad` above that top, capped at
    `max_lift`. Captions less than GROUP_GAP apart share the largest lift of their run.
    """
    import numpy as np

    frames = np.asarray(frames)
    _check_frames(frames, "no frames to place captions on")
    if len(times) != frames.shape[0]:
        raise ValueError("times and frames must have the same length")
    if base is None:
        from .captions import FILL_BOTTOM_SHARE

        base = FILL_BOTTOM_SHARE
    times = [float(moment) for moment in times]
    events = [(float(start), float(end)) for start, end in events]
    _, height, width = frames.shape
    left, right = _window(width, crop["x"], crop["keep"])
    columns = _window_columns(crop, width, left, right)
    if not columns.any():
        return [base] * len(events)
    density = _row_density(frames, left, right, columns)
    # The median of the rows, each averaged over every frame of the window.
    reference = float(np.median(density.mean(axis=0)))
    busy_level = BUSY_RATIO * reference
    tolerance = round(BAND_GAP * height)

    lifts = []
    for start, end in events:
        index = _frames_for(times, start, end)
        busy = density[index].mean(axis=0) > busy_level
        top = _band_top(busy, search, tolerance)
        lift = base
        if top is not None:
            edge = (height - top) / height
            if edge > base:
                lift = edge + pad
        lifts.append(min(lift, max_lift))
    return [round(lift, 4) for lift in _shared_max(events, lifts)]


def plan(source, start, end, events, *, keep=0.75):
    """Crop side and caption lifts for [start, end) of `source`; events are (start, end) seconds."""
    began = time.monotonic()
    times, frames = sample_frames(source, start, end)
    crop = choose_crop(frames, keep=keep)
    lifts = caption_lifts(times, frames, events, crop)
    return {
        "crop": crop,
        "lifts": lifts,
        "frames": len(times),
        "seconds": round(time.monotonic() - began, 3),
    }
