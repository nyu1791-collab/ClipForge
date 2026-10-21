"""Local, self-contained media only. No shell interpolation or input overwrites."""

import json
import math
from pathlib import Path
import re
import shutil
import subprocess

from .storage import PipelineError, process

PROTOCOLS = ["-protocol_whitelist", "file,pipe"]
FORMATS = ["-format_whitelist", "mov,matroska,avi,flv,mpeg,mpegts,ogg,asf"]
AUDIO_RATE = 48000
LOUDNESS = "I=-16:TP=-1.5:LRA=11"
MAX_KEEP_REGIONS = 60


def require_tools(captions=True):
    for name in ["ffmpeg", "ffprobe"]:
        if not shutil.which(name):
            raise PipelineError(name + " is not installed")
    if captions:
        output = process(["ffmpeg", "-hide_banner", "-filters"]).stdout.decode(
            errors="replace"
        )
        if not any(" ass " in line for line in output.splitlines()):
            raise PipelineError("FFmpeg must include the libass ass filter")


def probe(path):
    result = process(
        [
            "ffprobe",
            "-v",
            "error",
            *PROTOCOLS,
            *FORMATS,
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            Path(path).resolve(),
        ]
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if not video:
        raise PipelineError("input must contain a video stream")
    duration = float(data.get("format", {}).get("duration", video.get("duration", 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise PipelineError("video duration is unavailable")
    return {
        "duration": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def audio(source, destination, duration):
    result = process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-n",
            *PROTOCOLS,
            *FORMATS,
            "-i",
            source,
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            destination,
        ],
        timeout=max(180, duration * 2),
    )
    if result.stderr.strip():
        raise PipelineError("audio extraction reported a decoding error")


def preview(source, destination, candidate, settings):
    if destination.exists():
        destination.unlink()  # Derived previews only, never the input.
    result = process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-n",
            "-ss",
            f"{candidate.start:.6f}",
            *PROTOCOLS,
            *FORMATS,
            "-i",
            source,
            "-t",
            f"{candidate.end - candidate.start:.6f}",
            "-map",
            "0:V:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            "pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-r",
            "5",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            str(settings.threads),
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            "-movflags",
            "+faststart",
            destination,
        ],
        timeout=300,
    )
    if result.stderr.strip():
        raise PipelineError("preview reported a decoding error")
    return probe(destination)


def frame_size(settings):
    numerator, denominator = map(int, settings.aspect.split(":"))
    height = settings.height
    width = round(height * numerator / denominator / 2) * 2
    return width, height


def _box(box):
    """Crop expression for normalized x, y, width, height of the source frame."""
    x, y, w, h = box
    return f"crop=iw*{w:.6f}:ih*{h:.6f}:iw*{x:.6f}:ih*{y:.6f}"


def _video_timeline(clip, keep):
    if not keep:
        return "[0:V:0]setpts=PTS-STARTPTS[vin]"
    parts = [
        f"[0:V:0]trim=start={a - clip.start:.6f}:end={b - clip.start:.6f},"
        f"setpts=PTS-STARTPTS[vk{i}]"
        for i, (a, b) in enumerate(keep)
    ]
    labels = "".join(f"[vk{i}]" for i in range(len(keep)))
    return ";".join(parts) + f";{labels}concat=n={len(keep)}:v=1:a=0[vin]"


def _audio_timeline(clip, keep):
    if not keep:
        return "[0:a:0]asetpts=PTS-STARTPTS[ain]"
    parts = [
        f"[0:a:0]atrim=start={a - clip.start:.6f}:end={b - clip.start:.6f},"
        f"asetpts=PTS-STARTPTS[ak{i}]"
        for i, (a, b) in enumerate(keep)
    ]
    labels = "".join(f"[ak{i}]" for i in range(len(keep)))
    return ";".join(parts) + f";{labels}concat=n={len(keep)}:v=0:a=1[ain]"


def _layout(settings, width, height, src, out):
    """Turn [src] into [out] for the selected layout; split labels stay internal."""
    if settings.layout == "blur":
        # Blur at quarter size, then enlarge: a cheap background on CPU runners.
        small_w, small_h = max(2, width // 4), max(2, height // 4)
        return (
            f"[{src}]split=2[bgs][fgs];"
            f"[bgs]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
            f"crop={small_w}:{small_h},boxblur=8:2,scale={width}:{height},"
            "eq=brightness=-0.06:saturation=0.9[bg];"
            f"[fgs]scale={width}:{height}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,setsar=1[fg];"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,setsar=1[{out}]"
        )
    if settings.layout == "panels":
        game_h = round(height * 0.6 / 2) * 2
        face_h = height - game_h
        game = settings.game_box or (0.0, 0.0, 1.0, 1.0)
        return (
            f"[{src}]split=2[gs][fs];"
            f"[gs]{_box(game)},scale={width}:{game_h}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2,pad={width}:{game_h}:(ow-iw)/2:(oh-ih)/2:black,"
            f"setsar=1[gp];"
            f"[fs]{_box(settings.face_box)},scale={width}:{face_h}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{face_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[fp];"
            f"[gp][fp]vstack=inputs=2,setsar=1[{out}]"
        )
    mode = "decrease" if settings.framing == "fit" else "increase"
    fit = (
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        if settings.framing == "fit"
        else f"crop={width}:{height}"
    )
    return (
        f"[{src}]scale={width}:{height}:force_original_aspect_ratio={mode}:"
        f"force_divisible_by=2,{fit},setsar=1[{out}]"
    )


def _video_graph(clip, settings, width, height, keep, ass):
    zooms = settings.zoom
    labels = [f"b{i}" for i in range(1 + len(zooms))]
    chain = [_video_timeline(clip, keep)]
    if len(labels) > 1:
        chain.append(
            "[vin]split=" + str(len(labels)) + "".join(f"[{x}]" for x in labels)
        )
        base = labels[0]
    else:
        base = "vin"
    chain.append(_layout(settings, width, height, base, "lay0"))
    previous = "lay0"
    for index, zoom in enumerate(zooms):
        # Enlarged face shown over the frame while the zoom interval is active.
        hh = min(round(height * 0.8 / 2) * 2, round(height * 0.3 * zoom.scale / 2) * 2)
        chain.append(
            f"[{labels[index + 1]}]{_box(settings.face_box)},"
            f"scale=w='min({width},trunc(iw*{hh}/ih/2)*2)':h={hh},setsar=1[z{index}]"
        )
        nxt = f"lay{index + 1}"
        chain.append(
            f"[{previous}][z{index}]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2:"
            f"enable='between(t,{zoom.start:.3f},{zoom.end:.3f})'[{nxt}]"
        )
        previous = nxt
    if ass:
        # Captions are copied to a fixed basename beside the generated MP4.
        chain.append(f"[{previous}]ass=filename=captions.ass[vout]")
    else:
        chain.append(f"[{previous}]null[vout]")
    return ";".join(chain)


def _loudness_args(measured):
    text = f"loudnorm={LOUDNESS}"
    if measured:
        text += (
            f":measured_I={measured['input_i']:.2f}"
            f":measured_TP={measured['input_tp']:.2f}"
            f":measured_LRA={measured['input_lra']:.2f}"
            f":measured_thresh={measured['input_thresh']:.2f}"
            f":offset={measured['target_offset']:.2f}:linear=true"
        )
    return text


def measure_loudness(source, clip, keep):
    """First pass: measure the same audio chain that the render will use."""
    graph = (
        _audio_timeline(clip, keep)
        + f";[ain]loudnorm={LOUDNESS}:print_format=json,aresample={AUDIO_RATE}[aout]"
    )
    duration = clip.end - clip.start
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            "-ss",
            f"{clip.start:.6f}",
            "-i",
            str(Path(source).resolve()),
            "-t",
            f"{duration:.6f}",
            "-filter_complex",
            graph,
            "-map",
            "[aout]",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=max(300, duration * 4),
        check=False,
    )
    blocks = re.findall(r"\{[^{}]*\}", result.stderr)
    if result.returncode or not blocks:
        raise PipelineError("loudness measurement failed")
    data = json.loads(blocks[-1])
    values = {}
    for key in ["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"]:
        number = float(data[key])
        if not math.isfinite(number):
            raise PipelineError("loudness measurement is not finite")
        values[key] = number
    return values


def render(source, destination, clip, settings, ass=None, keep=None):
    width, height = frame_size(settings)
    duration = sum(b - a for a, b in keep) if keep else clip.end - clip.start
    for zoom in settings.zoom:
        if zoom.end > duration + 0.01:
            raise PipelineError("zoom interval is outside the rendered clip")
    if keep and len(keep) > MAX_KEEP_REGIONS:
        raise PipelineError("silence cut produced too many regions")
    has_audio = probe(source)["audio"]
    graph = [_video_graph(clip, settings, width, height, keep, ass)]
    if has_audio:
        measured = (
            measure_loudness(source, clip, keep)
            if settings.loudness_two_pass
            else None
        )
        graph.append(
            _audio_timeline(clip, keep)
            + f";[ain]{_loudness_args(measured)},aresample={AUDIO_RATE}[aout]"
        )
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-n",
        "-ss",
        f"{clip.start:.6f}",
        *PROTOCOLS,
        *FORMATS,
        "-i",
        str(Path(source).resolve()),
        "-t",
        f"{clip.end - clip.start:.6f}",
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[vout]",
    ]
    if has_audio:
        command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_RATE)]
    command += [
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(settings.threads),
        "-movflags",
        "+faststart",
        str(Path(destination).resolve()),
    ]
    result = subprocess.run(
        command,
        cwd=Path(destination).parent,
        capture_output=True,
        text=True,
        timeout=max(300, duration * 20),
    )
    if result.returncode or result.stderr.strip():
        raise PipelineError("clip rendering failed")
    info = probe(destination)
    if abs(info["duration"] - duration) > 0.3:
        raise PipelineError("rendered duration does not match the selection")
    if (info["width"], info["height"]) != (width, height):
        raise PipelineError("rendered dimensions do not match the requested layout")
    return info
