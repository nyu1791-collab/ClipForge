"""Cut an explicit time range from a local video with FFmpeg."""

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CutResult:
    input_path: str
    output_path: str
    start: float
    end: float
    requested_duration: float
    output_duration: float


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{command[0]} failed: {completed.stderr.strip()}")
    return completed


def probe_duration(path: Path) -> float:
    completed = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    )
    duration = json.loads(completed.stdout).get("format", {}).get("duration")
    if duration is None:
        raise RuntimeError(f"duration is not available: {path}")
    return float(duration)


def validate_range(start: float, end: float, duration: float) -> None:
    if not (math.isfinite(start) and math.isfinite(end)):
        raise ValueError("start and end must be finite numbers of seconds")
    if start < 0:
        raise ValueError("start must be 0 seconds or later")
    if end <= start:
        raise ValueError("end must be later than start")
    if end > duration:
        raise ValueError(f"end ({end:.3f}s) is beyond the video duration ({duration:.3f}s)")


def cut_clip(input_path: Path, output_path: Path, start: float, end: float) -> CutResult:
    source = input_path.resolve(strict=True)
    target = output_path.resolve()
    if not source.is_file():
        raise ValueError(f"input is not a file: {source}")
    if target == source:
        raise ValueError("output must differ from input; the source video is never overwritten")
    if target.suffix.lower() != ".mp4":
        raise ValueError("output must be an .mp4 file")
    if target.exists():
        raise FileExistsError(f"output already exists: {target}")

    duration = probe_duration(source)
    validate_range(start, end, duration)

    target.parent.mkdir(parents=True, exist_ok=True)
    # Re-encoding keeps the cut frame-accurate; stream copy would snap to keyframes.
    _run(
        [
            "ffmpeg", "-n", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(source),
            "-t", f"{end - start:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(target),
        ]
    )
    return CutResult(
        input_path=str(source),
        output_path=str(target),
        start=start,
        end=end,
        requested_duration=end - start,
        output_duration=probe_duration(target),
    )
