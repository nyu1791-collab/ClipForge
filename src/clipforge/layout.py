"""Static source regions and bounded, clip-relative FFmpeg compositions.

The blur-fit filter arrangement is adapted from Cutawan (MIT).
Copyright (c) 2026 Cutawan Contributors. See third_party/Cutawan-LICENSE.
Unlike a tracking camera, a region stays fixed until an explicit punch-in cut.
"""

from dataclasses import asdict, dataclass
import math

from .storage import PipelineError


def even(value):
    return max(2, int(round(value / 2)) * 2)


def output_size(settings):
    numerator, denominator = map(int, settings.aspect.split(":"))
    return even(settings.height * numerator / denominator), settings.height


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


def source_rect(region, source_width, source_height, zoom=1):
    """Round inwards to chroma-safe pixels without reading outside the source."""
    if region is None:
        x, y, right, bottom = 0, 0, source_width, source_height
    else:
        x = math.floor(region.x * source_width / 2) * 2
        y = math.floor(region.y * source_height / 2) * 2
        right = math.floor(min(1, region.x + region.width) * source_width / 2) * 2
        bottom = math.floor(min(1, region.y + region.height) * source_height / 2) * 2
    width, height = (right - x) // 2 * 2, (bottom - y) // 2 * 2
    if width < 4 or height < 4:
        raise PipelineError("source region is smaller than four pixels")
    zw, zh = math.floor(width / zoom / 2) * 2, math.floor(height / zoom / 2) * 2
    if zw < 4 or zh < 4:
        raise PipelineError("zoomed source region is too small")
    return Rect(x + (width - zw) // 4 * 2, y + (height - zh) // 4 * 2, zw, zh)


def panel(width, height, x, y, w, h):
    return Rect(
        int(x * width / 2) * 2,
        int(y * height / 2) * 2,
        even(w * width),
        even(h * height),
    )


def filter_graph(info, clip, settings, subtitles=False):
    width, height = output_size(settings)
    graph = []
    layers = []
    active = []
    if settings.framing == "stacked":
        # Reserve the top for a two-line hook, and the bottom for two-line captions.
        layers = [
            (
                "content",
                settings.content_region,
                panel(width, height, 0.04, 0.23, 0.92, 0.29),
                1,
                None,
            ),
            (
                "presenter",
                settings.presenter_region,
                panel(width, height, 0.14, 0.54, 0.72, 0.20),
                1,
                None,
            ),
        ]
        for punch in settings.punch_ins:
            start, end = max(clip.start, punch.start), min(clip.end, punch.end)
            if end > start:
                local = (start - clip.start, end - clip.start)
                layers.append(
                    (
                        "presenter",
                        settings.presenter_region,
                        layers[1][2],
                        punch.zoom,
                        local,
                    )
                )
                active.append({"start": local[0], "end": local[1], "zoom": punch.zoom})
    else:
        layers = [("content", None, Rect(0, 0, width, height), 1, None)]

    use_background = settings.framing in {"blur-fit", "stacked"}
    if use_background:
        graph.append(
            f"[0:{info['video_index']}]setpts=PTS-STARTPTS,setsar=1,"
            f"split={len(layers) + 1}[background]"
            + "".join(f"[source{i}]" for i in range(len(layers)))
        )
        # Blur a quarter-size background to bound CPU and peak filter memory.
        bw, bh = even(width / 4), even(height / 4)
        sigma = max(2, round(height / 1920 * 12, 2))
        graph.append(
            f"[background]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh},gblur=sigma={sigma},eq=brightness=-0.12,"
            f"scale={width}:{height}[base]"
        )
    else:
        graph.append(f"[0:{info['video_index']}]setpts=PTS-STARTPTS,setsar=1[source0]")

    regions, warnings = [], []
    previous = "base"
    for index, (role, region, dest, zoom, interval) in enumerate(layers):
        rect = source_rect(region, info["width"], info["height"], zoom)
        filters = [f"crop={rect.width}:{rect.height}:{rect.x}:{rect.y}"]
        if settings.framing == "center-crop":
            filters += [
                f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2",
                f"crop={width}:{height}",
            ]
        else:
            filters += [
                f"scale={dest.width}:{dest.height}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos",
                "setsar=1",
            ]
            if not use_background:
                filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
        graph.append(f"[source{index}]" + ",".join(filters) + f"[layer{index}]")
        if use_background:
            enabled = ""
            if interval:
                enabled = f":enable='gte(t,{interval[0]:.6f})*lt(t,{interval[1]:.6f})'"
            graph.append(
                f"[{previous}][layer{index}]overlay="
                f"{dest.x}+({dest.width}-w)/2:{dest.y}+({dest.height}-h)/2"
                f":eof_action=pass{enabled}[composite{index}]"
            )
            previous = f"composite{index}"
        else:
            previous = f"layer{index}"
        regions.append(
            {
                "role": role,
                "source": asdict(rect),
                "destination": asdict(dest),
                "zoom": zoom,
                "local_interval": interval,
            }
        )
        if min(dest.width / rect.width, dest.height / rect.height) > 2:
            warnings.append(f"{role}_upscaled_over_2x")
    tail = "setsar=1"
    if subtitles:
        tail += ",ass=filename=captions.ass"
    graph.append(f"[{previous}]{tail}[video]")
    return ";".join(graph), {
        "framing": settings.framing,
        "width": width,
        "height": height,
        "regions": regions,
        "punch_ins": active,
        "review_required": sorted(set(warnings)),
    }
