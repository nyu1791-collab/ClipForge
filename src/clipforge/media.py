"""Local, self-contained media only. No shell interpolation or input overwrites."""

import json
import math
from pathlib import Path
import shutil

from .storage import PipelineError, process

PROTOCOLS = ["-protocol_whitelist", "file,pipe"]
FORMATS = ["-format_whitelist", "mov,matroska,avi,flv,mpeg,mpegts,ogg,asf"]


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


def render(source, destination, clip, settings, ass=None):
    numerator, denominator = map(int, settings.aspect.split(":"))
    height = settings.height
    width = round(height * numerator / denominator / 2) * 2
    mode = "decrease" if settings.framing == "fit" else "increase"
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio={mode}:force_divisible_by=2"
    ]
    if settings.framing == "fit":
        filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    else:
        filters.append(f"crop={width}:{height}")
    filters += ["setsar=1", "setpts=PTS-STARTPTS"]
    if ass:
        # Captions are copied to a fixed basename beside the generated MP4.
        filters.append("ass=filename=captions.ass")
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
        "-map",
        "0:V:0",
        "-map",
        "0:a:0?",
        "-vf",
        ",".join(filters),
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
    ]
    if probe(source)["audio"]:
        command += [
            "-af",
            "asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
        ]
    command += ["-movflags", "+faststart", str(Path(destination).resolve())]
    import subprocess

    result = subprocess.run(
        command,
        cwd=Path(destination).parent,
        capture_output=True,
        timeout=max(300, (clip.end - clip.start) * 20),
    )
    if result.returncode or result.stderr.strip():
        raise PipelineError("clip rendering failed")
    info = probe(destination)
    if abs(info["duration"] - (clip.end - clip.start)) > 0.3:
        raise PipelineError("rendered duration does not match the selection")
    if (info["width"], info["height"]) != (width, height):
        raise PipelineError("rendered dimensions do not match the requested layout")
    return info
