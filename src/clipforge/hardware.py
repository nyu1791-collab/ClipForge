"""Resolve local hardware without importing PyTorch or downloading a model."""

from functools import lru_cache
import os
import subprocess

from .storage import PipelineError


def encode_threads(settings):
    """Rendering uses this machine's available cores."""
    return settings.encode_threads or _available_cpu_threads()


def _available_cpu_threads():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


@lru_cache(maxsize=1)
def nvenc_available():
    # An encoder listing alone does not prove a working driver/NVENC session.
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                # Turing H.264 NVENC requires width >=145; a 128px probe rejects T4.
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=30",
                "-frames:v",
                "1",
                "-an",
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p1",
                "-rc",
                "vbr",
                "-cq",
                "20",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def video_options(settings, quality):
    encoder = settings.video_encoder
    if encoder in {"auto", "h264_nvenc"}:
        available = nvenc_available()
        if encoder == "h264_nvenc" and not available:
            raise PipelineError(
                "FFmpeg h264_nvenc is unavailable; NVENC execution required"
            )
        encoder = "h264_nvenc" if available else "libx264"
    if encoder == "h264_nvenc":
        # p5 + hq tuning + spatial AQ: visibly cleaner text edges and gradients at the
        # same constant-quality target; still far faster than real time at 360-1080p.
        options = [
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(quality),
            "-b:v",
            "0",
            "-spatial-aq",
            "1",
            "-rc-lookahead",
            "20",
        ]
    else:
        options = ["-preset", "veryfast", "-crf", str(quality)]
    return [
        "-c:v",
        encoder,
        *options,
        "-pix_fmt",
        "yuv420p",
        # Tag SDR output as BT.709 so players do not guess and wash out colours.
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-threads",
        str(encode_threads(settings)),
    ]
