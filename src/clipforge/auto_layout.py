"""Choose the crop side and caption band for the landscape chat layout from frame content.

The 16:9 source keeps `keep` of its width (the left 1440x1080 of a 1920x1080 frame). The
crop side is the window that keeps the most detail (gradient energy). Captions stay at the
bottom; a caption is lifted just above a busy band (HUD, hotbar, overlay) that touches the
bottom edge, up to MAX_LIFT of the height.
"""

import time
import subprocess
import tempfile
import threading
from dataclasses import dataclass

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
# Captions this close (seconds) in sequence share one band, so the band does not flicker.
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
    """Iterate (source times, grayscale frames) in blocks of at most BLOCK frames."""
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
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
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
    return _decode_frames(
        command, start, fps, width, height, max(120, int((end - start) * 2) + 60)
    )


def _decode_frames(command, start, fps, width, height, timeout):
    import numpy as np

    count, size = 0, width * height
    expired = threading.Event()
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors
            )
        except OSError:
            raise PipelineError("layout frame decoder could not start") from None

        def stop():
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, stop)
        timer.daemon = True
        timer.start()
        try:
            while True:
                data = proc.stdout.read(BLOCK * size)
                number = len(data) // size
                if not number:
                    break
                frames = np.frombuffer(
                    data, dtype=np.uint8, count=number * size
                ).reshape(number, height, width)
                yield [start + (count + i) / fps for i in range(number)], frames
                count += number
            code = proc.wait()
            if expired.is_set():
                raise PipelineError("layout frame decoding timed out")
            if code:
                raise PipelineError("layout frame decoding failed")
            if not count:
                raise PipelineError("no video frames in the layout sample window")
        finally:
            timer.cancel()
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@dataclass
class FrameStatistics:
    count: int
    height: int
    width: int
    profile: object
    dark: object
    mean_gradient: object
    persistent: object
    times: list
    bottom: object
    top: object
    rows: int


def frame_statistics(samples, *, band=0.24):
    """Keep column/band summaries, never the full decoded video or its gradients."""
    import numpy as np

    # Older callers can supply one already-decoded (times, frames) pair.
    if isinstance(samples, tuple):
        samples = [samples]
    count, times, bottoms, tops = 0, [], [], []
    height = width = rows = None
    for moments, frames in samples:
        _check_frames(frames, "no frames to analyse")
        if len(moments) != len(frames):
            raise ValueError("times and frames must have the same length")
        if height is None:
            height, width = frames.shape[1:]
            rows = max(1, round(band * height))
            profile, dark = np.zeros(width), np.zeros(width)
            gradients, edges = np.zeros((height, width)), np.zeros((height, width))
        if frames.shape[1:] != (height, width):
            raise ValueError("sample frame dimensions changed")
        for offset in range(0, len(frames), BLOCK):
            block = frames[offset : offset + BLOCK]
            gradient = _gradient(block)
            profile += gradient.sum(axis=(0, 1))
            dark += (block < DARK_LEVEL).sum(axis=(0, 1))
            gradients += gradient.sum(axis=0)
            edges += (gradient > 80).sum(axis=0)
            bottoms.append(gradient[:, height - rows :, :].mean(axis=1))
            tops.append(gradient[:, :rows, :].mean(axis=1))
        count += len(frames)
        times.extend(float(t) for t in moments)
    if not count:
        raise PipelineError("no video frames in the layout sample window")
    return FrameStatistics(
        count,
        height,
        width,
        profile / count,
        dark / (count * height),
        gradients / count,
        edges / count,
        times,
        np.concatenate(bottoms),
        np.concatenate(tops),
        rows,
    )


def _statistics(frames):
    import numpy as np

    if isinstance(frames, FrameStatistics):
        return frames
    frames = np.asarray(frames)
    _check_frames(frames, "no frames to choose a crop from")
    return frame_statistics((list(range(len(frames))), frames))


def _runs(mask):
    """One representative pixel per contiguous line candidate."""
    positions, previous = [], -2
    for value in mask.nonzero()[0]:
        if value > previous + 1:
            positions.append(int(value))
        previous = value
    return positions


def _corner_wipe(stats):
    """Persistent closed rectangles in a lower corner, not transient game texture."""
    import numpy as np

    if stats.count < 3:
        return None
    h, w = stats.height, stats.width
    energy = stats.mean_gradient
    stable = stats.persistent >= 0.8
    horizontal = stable & (
        energy > 1.5 * (np.roll(energy, 4, 0) + np.roll(energy, -4, 0)) / 2 + 40
    )
    vertical = stable & (
        energy > 1.5 * (np.roll(energy, 4, 1) + np.roll(energy, -4, 1)) / 2 + 40
    )
    candidates = []
    for side, lo, hi in (("left", 0, round(w * 0.36)), ("right", round(w * 0.64), w)):
        ys = _runs(horizontal[:, lo:hi].mean(axis=1) > 0.2)
        xs = _runs(vertical[round(h * 0.5) :, :].mean(axis=0) > 0.2)
        xs = [x for x in xs if lo <= x < hi]
        for left in xs:
            for right in xs:
                if not 0.1 * w <= right - left <= 0.35 * w:
                    continue
                if (
                    side == "left"
                    and left > 0.08 * w
                    or side == "right"
                    and right < 0.92 * w
                ):
                    continue
                for top in ys:
                    if not 0.5 * h <= top <= 0.85 * h:
                        continue
                    for bottom in ys:
                        if bottom < 0.85 * h or not 0.1 * h <= bottom - top <= 0.45 * h:
                            continue
                        support = [
                            horizontal[y : y + 2, left : right + 1].any(axis=0).mean()
                            for y in (top, bottom)
                        ]
                        support += [
                            vertical[top : bottom + 1, x : x + 2].any(axis=1).mean()
                            for x in (left, right)
                        ]
                        if min(support) >= 0.75:
                            candidates.append(
                                (
                                    sum(support),
                                    (right - left) * (bottom - top),
                                    side,
                                    left,
                                    right,
                                    top,
                                    bottom,
                                )
                            )
    if not candidates:
        return None
    score, _, side, left, right, top, bottom = max(candidates)
    return {
        "side": side,
        "left": max(0, left - 2) / w,
        "right": min(w, right + 3) / w,
        "top": max(0, top - 2) / h,
        "bottom": min(h, bottom + 3) / h,
        "confidence": round(score / 4, 3),
    }


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
    if not 0 < keep <= 1:
        raise ValueError("keep must be in (0, 1]")
    stats = _statistics(frames)
    width = stats.width
    profile, dark = stats.profile, stats.dark
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

    wipe = _corner_wipe(stats)
    if wipe is not None and wipe["right"] - wipe["left"] <= keep:
        # Keeping the camera rectangle takes priority over excluding the chat overlay.
        low = max(0.0, wipe["right"] - keep)
        high = min(slack, wipe["left"])
        x = min(max(x, low), high)

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
        "wipe": wipe,
    }


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
    GROUP_GAP apart share the largest lift of their run.

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
    return [round(lift, 4) for lift in _shared_max(events, lifts)], bands


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


def _stream_analysis(stats, samples, events, crop):
    """Exact persistent medians with a bounded rolling frame cache and row summaries.

    Crop statistics are known from the first decode. The second decode keeps at most BLOCK
    selected local frames and PERSIST_MAX global samples, not the whole clip. Completed caption
    windows retain only three row profiles. The native array API uses the same median and masks.
    """
    from collections import Counter
    import numpy as np
    from .captions import FILL_BOTTOM_SHARE

    base = FILL_BOTTOM_SHARE
    if not events:
        return [], []
    height, width = stats.height, stats.width
    left, right = _window(width, crop["x"], crop["keep"])
    columns = _window_columns(crop, width, left, right)
    if not columns.any():
        return [base] * len(events), [[] for _ in events]
    positions = np.arange(right - left)
    middle = (right - left) // 2
    regions = [
        ("whole", columns),
        ("left", columns & (positions < middle)),
        ("right", columns & (positions >= middle)),
    ]
    regions = [(name, mask) for name, mask in regions if mask.any()]
    masks = [mask for _, mask in regions]
    selections = [_caption_frames(stats.times, start, end) for start, end in events]
    required = Counter(int(index) for indices in selections for index in indices)
    order = sorted(range(len(events)), key=lambda i: int(selections[i][-1]))
    global_indices = set(int(i) for i in _subsample(np.arange(stats.count)))
    local, global_frames = {}, {}
    profiles = [None] * len(events)
    cursor = ready = 0
    if isinstance(samples, tuple):
        samples = [samples]
    for _, frames in samples:
        for frame in frames:
            if cursor in required or cursor in global_indices:
                copy = frame.copy()
                if cursor in required:
                    local[cursor] = copy
                if cursor in global_indices:
                    global_frames[cursor] = copy
            while ready < len(order) and selections[order[ready]][-1] <= cursor:
                item = order[ready]
                indices = selections[item]
                if any(int(i) not in local for i in indices):
                    raise PipelineError("caption frame samples changed between decodes")
                chosen = np.stack([local[int(i)] for i in indices])
                persistent = _persistent_gradient(chosen, np.arange(len(chosen)))
                profiles[item] = [
                    _region_profile(persistent, left, right, mask) for mask in masks
                ]
                for index in indices:
                    required[int(index)] -= 1
                    if not required[int(index)]:
                        del required[int(index)]
                        del local[int(index)]
                ready += 1
            if len(local) > BLOCK:
                raise PipelineError("caption sample exceeds its bounded frame window")
            cursor += 1
    if (
        cursor != stats.count
        or ready != len(events)
        or len(global_frames) != len(global_indices)
    ):
        raise PipelineError("layout frame samples changed between decodes")
    chosen = np.stack([global_frames[i] for i in sorted(global_frames)])
    whole = _persistent_gradient(chosen, np.arange(len(chosen)))
    references = [
        float(np.median(_region_profile(whole, left, right, mask))) for mask in masks
    ]
    tolerance = round(BAND_GAP * height)
    lifts, bands = [], []
    for row_profiles in profiles:
        lift, found = base, []
        for (name, _), profile, reference in zip(regions, row_profiles, references):
            busy = profile > BUSY_RATIO * reference
            found.extend(_busy_runs(busy, height, name))
            top = _band_top(busy, SEARCH, tolerance)
            if top is not None:
                edge = (height - top) / height
                if edge > base:
                    lift = max(lift, edge + LIFT_PAD)
        lifts.append(min(lift, MAX_LIFT))
        bands.append(sorted(found, key=lambda run: (run[0], run[1], run[2])))
    return [round(lift, 4) for lift in _shared_max(events, lifts)], bands


def _stream_lifts(stats, samples, events, crop):
    """Compatibility helper returning the lifts from the bounded analysis."""
    return _stream_analysis(stats, samples, events, crop)[0]


def plan(source, start, end, events, *, keep=0.75, anchor="auto"):
    """Bounded crop statistics and persistent bottom-caption lifts in source seconds."""
    began = time.monotonic()
    stats = frame_statistics(sample_frames(source, start, end))
    crop = choose_crop(stats, keep=keep)
    if anchor != "auto":
        crop.update(
            anchor=anchor,
            x={"left": 0.0, "center": (1 - keep) / 2, "right": 1 - keep}[anchor],
        )
    lifts, bands = _stream_analysis(
        stats, sample_frames(source, start, end), events, crop
    )
    return {
        "crop": crop,
        "lifts": lifts,
        "bands": bands,
        "frames": stats.count,
        "seconds": round(time.monotonic() - began, 3),
    }
