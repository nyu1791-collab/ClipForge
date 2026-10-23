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
# Neighbouring captions this close (seconds, gap between them) share the higher lift, so a caption does not flicker.
GROUP_GAP = 2.0
# A row is busy when its density is above this multiple of the window's median row density.
BUSY_RATIO = 1.6
# Busy rows are searched from the bottom edge up to this share of the height.
SEARCH = 0.40
# Quiet rows inside a busy band are tolerated up to this share of the height.
BAND_GAP = 0.015
# A caption's persistent detail is the temporal median over the frames within this many seconds of
# it, at least PERSIST_MIN frames (the nearest ones are added), at most PERSIST_MAX frames.
PERSIST_MARGIN = 3.0
PERSIST_MIN = 5
PERSIST_MAX = 64
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


def _neighbour_max(events, values):
    """Each caption takes the largest value of itself and of the captions next to it in time.

    Captions are taken in start order. The next caption is a neighbour when it starts less than
    GROUP_GAP after the latest end so far (an overlap counts as a gap below it). Only the directly
    adjacent captions are compared, so the rise does not spread along a long conversation.
    """
    order = sorted(range(len(events)), key=lambda index: events[index])
    linked, reach = [], float("-inf")
    for position, index in enumerate(order):
        start, end = events[index]
        if position:
            linked.append(start - reach < GROUP_GAP)
        reach = max(reach, end)
    result = list(values)
    for position, index in enumerate(order):
        peers = [values[index]]
        if position and linked[position - 1]:
            peers.append(values[order[position - 1]])
        if position < len(order) - 1 and linked[position]:
            peers.append(values[order[position + 1]])
        result[index] = max(peers)
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


def _caption_frames(times, start, end):
    """Indices of the frames near [start, end] for a caption's persistent detail.

    Frames within PERSIST_MARGIN seconds of the caption count; if fewer than PERSIST_MIN, the
    nearest frames in time are added. Long spans are subsampled to PERSIST_MAX frames.
    """
    import numpy as np

    moments = np.asarray(times, dtype=np.float64)
    distances = np.maximum.reduce(
        [
            start - PERSIST_MARGIN - moments,
            moments - end - PERSIST_MARGIN,
            np.zeros(len(moments)),
        ]
    )
    inside = int((distances == 0).sum())
    count = min(len(moments), max(PERSIST_MIN, inside))
    order = np.argsort(distances, kind="stable")[:count]
    return _subsample(np.sort(order))


def _subsample(indices):
    """At most PERSIST_MAX of the given frame indices, spread evenly."""
    import numpy as np

    indices = np.asarray(indices, dtype=np.int64)
    if len(indices) <= PERSIST_MAX:
        return indices
    picks = np.linspace(0, len(indices) - 1, PERSIST_MAX).round().astype(int)
    return indices[picks]


def _persistent_gradient(frames, indices):
    """Per-pixel temporal median of the gradient over the frames at `indices`.

    Detail that stays in place (a HUD, a wipe's frame, a border) keeps its gradient; detail
    that moves with the game world drops out of the median.
    """
    import numpy as np

    return np.median(_gradient(frames[indices]), axis=0)


def _region_profile(persistent, left, right, mask):
    """Mean gradient of each row over the window columns selected by `mask`: shape [height]."""
    return persistent[:, left:right][:, mask].mean(axis=1)


def _busy_runs(busy, height, region):
    """Runs of busy rows as [bottom, top, region], shares of the height counted from the bottom."""
    runs, first = [], None
    for row in range(len(busy) + 1):
        flagged = row < len(busy) and bool(busy[row])
        if flagged and first is None:
            first = row
        elif not flagged and first is not None:
            bottom = (height - row) / height
            top = (height - first) / height
            runs.append([round(bottom, 4), round(top, 4), region])
            first = None
    return runs


def caption_analysis(
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
    """Bottom margin of each caption ("lift", a share of the height) and its static bands.

    For each caption, the gradient is the temporal median over the frames near it, so only detail
    that stays in place counts (a HUD, a wipe, a border; not the moving game world). A row is busy
    when its persistent density is above BUSY_RATIO times the median of the window's rows, taken
    over the whole sample. The window is measured whole and in its left and right halves; a band
    found in any of them counts. A caption sits at the base margin unless a busy band touches the
    bottom edge (searched up to `search` of the height) and the band's top is above the base; then
    the caption's bottom edge sits `pad` above that top, capped at `max_lift`. Captions less than
    GROUP_GAP apart take the higher of their two lifts (only the adjacent caption counts).

    Returns (lifts, bands). bands[i] lists every run of busy rows that caption i was judged on, as
    [bottom, top, region] with region "whole", "left" or "right"; the quality check uses them.
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
        return [base] * len(events), [[] for _ in events]
    span = right - left
    position = np.arange(span)
    middle = span // 2
    regions = [
        ("whole", columns),
        ("left", columns & (position < middle)),
        ("right", columns & (position >= middle)),
    ]
    regions = [(name, mask) for name, mask in regions if mask.any()]

    whole = _persistent_gradient(frames, _subsample(np.arange(len(times))))
    references = [
        float(np.median(_region_profile(whole, left, right, mask)))
        for _, mask in regions
    ]
    tolerance = round(BAND_GAP * height)

    lifts, bands = [], []
    for start, end in events:
        indices = _caption_frames(times, start, end)
        persistent = _persistent_gradient(frames, indices)
        lift = base
        found = []
        for (name, mask), reference in zip(regions, references):
            profile = _region_profile(persistent, left, right, mask)
            busy = profile > BUSY_RATIO * reference
            found.extend(_busy_runs(busy, height, name))
            top = _band_top(busy, search, tolerance)
            if top is not None:
                edge = (height - top) / height
                if edge > base:
                    lift = max(lift, edge + pad)
        lifts.append(min(lift, max_lift))
        bands.append(sorted(found, key=lambda run: (run[0], run[1], run[2])))
    return [round(lift, 4) for lift in _neighbour_max(events, lifts)], bands


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
    """The lifts of caption_analysis, without the bands."""
    lifts, _ = caption_analysis(
        times,
        frames,
        events,
        crop,
        search=search,
        max_lift=max_lift,
        base=base,
        pad=pad,
    )
    return lifts


def apply_floor(lifts, floor):
    """Each lift raised to at least `floor`, a caption floor reviewed for one clip (wipes the detector misses)."""
    if not 0 <= floor <= MAX_LIFT:
        raise ValueError("caption floor must be from 0 to MAX_LIFT")
    return [round(min(max(lift, floor), MAX_LIFT), 4) for lift in lifts]


def plan(source, start, end, events, *, keep=0.75):
    """Crop side and caption lifts for [start, end) of `source`; events are (start, end) seconds."""
    began = time.monotonic()
    times, frames = sample_frames(source, start, end)
    crop = choose_crop(frames, keep=keep)
    lifts, bands = caption_analysis(times, frames, events, crop)
    return {
        "crop": crop,
        "lifts": lifts,
        "bands": bands,
        "frames": len(times),
        "seconds": round(time.monotonic() - began, 3),
    }
