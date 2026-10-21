"""Local, self-contained media only. No shell interpolation or input overwrites."""

import json
import math
from pathlib import Path
import shutil

from .layout import filter_graph, output_size
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
    rotation = next(
        (
            s.get("rotation", 0)
            for s in video.get("side_data_list", [])
            if "rotation" in s
        ),
        video.get("tags", {}).get("rotate", 0),
    )
    width, height = int(video["width"]), int(video["height"])
    if round(float(rotation)) % 180 == 90:
        width, height = height, width
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "video_index": int(video["index"]),
        "video_streams": sum(s.get("codec_type") == "video" for s in streams),
        "audio_streams": sum(s.get("codec_type") == "audio" for s in streams),
        "sample_aspect_ratio": video.get("sample_aspect_ratio", "1:1"),
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
    source_info = probe(source)
    width, height = output_size(settings)
    graph, layout = filter_graph(source_info, clip, settings, subtitles=ass is not None)
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
        "[video]",
        "-map",
        "0:a:0?",
        "-filter_complex_threads",
        str(settings.threads),
        "-filter_complex",
        graph,
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
    if source_info["audio"]:
        command += [
            "-af",
            "asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
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
    if info["video_streams"] != 1 or info["audio_streams"] != int(source_info["audio"]):
        raise PipelineError("rendered stream count does not match the source")
    # A successful encode/probe alone does not establish a decodable deliverable.
    decoded = process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-xerror",
            *PROTOCOLS,
            *FORMATS,
            "-i",
            str(Path(destination).resolve()),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-f",
            "null",
            "-",
        ],
        timeout=max(120, (clip.end - clip.start) * 4),
    )
    if decoded.stderr.strip():
        raise PipelineError("rendered output reported a decoding error")
    if source_info["sample_aspect_ratio"] not in {"1:1", "0:1", "N/A"}:
        layout["review_required"].append("non_square_source_pixels")
    info.update(
        layout=layout,
        full_decode_verified=True,
        audio_normalization={
            "target_lufs": -16,
            "target_true_peak_db": -1.5,
            "method": "single-pass-loudnorm",
        }
        if source_info["audio"]
        else None,
    )
    return info
