"""Choose the crop side and caption band for the landscape chat layout from frame content.

The 16:9 source keeps `keep` of its width (the left 1440x1080 of a 1920x1080 frame). The
crop side is the window that keeps the most detail (gradient energy). A caption moves to
the top band only when the bottom band is busy and the caption is not a quiet moment.
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


def _band_densities(frames, left, right, rows, overlay=None):
    """Band density over content columns only; source chat text is not scene detail."""
    import numpy as np

    stats = _statistics(frames)
    columns = np.arange(left, right)
    if overlay and overlay.get("side"):
        lo, hi = (
            round(overlay["start"] * stats.width),
            round(overlay["end"] * stats.width),
        )
        columns = columns[(columns < lo) | (columns >= hi)]
    if not len(columns):
        return np.zeros(stats.count), np.zeros(stats.count)
    if stats.rows != rows:
        raise ValueError("caption band differs from sampled statistics")
    return stats.bottom[:, columns].mean(axis=1), stats.top[:, columns].mean(axis=1)


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

    if not isinstance(frames, FrameStatistics):
        frames = frame_statistics((times, np.asarray(frames)), band=band)
    if len(times) != frames.count:
        raise ValueError("times and frames must have the same length")
    times = [float(moment) for moment in times]
    height, width = frames.height, frames.width
    left, right = _window(width, crop["x"], crop["keep"])
    rows = max(1, round(band * height))
    overlay = crop.get("overlay")
    if overlay is None:
        block = _overlay_block(frames.dark >= DARK_SHARE, width)
        overlay = (
            {"side": block[0], "start": block[1] / width, "end": block[2] / width}
            if block
            else None
        )
    bottom, top = _band_densities(frames, left, right, rows, overlay)
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


def plan(source, start, end, events, *, keep=0.75, anchor="auto"):
    """Crop side and caption bands for [start, end) of `source`; events are (start, end) seconds."""
    began = time.monotonic()
    frames = frame_statistics(sample_frames(source, start, end))
    times = frames.times
    crop = choose_crop(frames, keep=keep)
    if anchor != "auto":
        crop.update(
            anchor=anchor,
            x={"left": 0.0, "center": (1 - keep) / 2, "right": 1 - keep}[anchor],
        )
    positions = caption_positions(times, frames, events, crop)
    return {
        "crop": crop,
        "positions": positions,
        "frames": len(times),
        "seconds": round(time.monotonic() - began, 3),
    }
