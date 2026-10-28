"""Advisory audio levels in transcription gaps; levels are not speech detection."""

import subprocess
import numpy as np


def describe_gaps(audio, duration, segments, min_gap=10):
    if duration > 3600:
        return {
            "status": "unavailable",
            "reason": "duration_above_diagnostic_limit",
            "gaps": [],
        }
    intervals = []
    end = 0.0
    for row in sorted(segments, key=lambda row: (row["start"], row["end"])):
        start = max(0.0, min(duration, row["start"]))
        if start - end >= min_gap:
            intervals.append((end, start))
        end = max(end, min(duration, row["end"]))
    if duration - end >= min_gap:
        intervals.append((end, duration))
    if not intervals:
        return {
            "status": "complete",
            "gaps": [],
            "note": "Audio level alone cannot identify speech.",
        }
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(audio),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "f32le",
                "pipe:1",
            ],
            capture_output=True,
            timeout=max(60, int(duration / 2)),
            check=False,
        )
        if proc.returncode:
            return {
                "status": "unavailable",
                "reason": "audio_decode_failed",
                "gaps": [],
            }
        pcm = np.frombuffer(proc.stdout[: len(proc.stdout) // 4 * 4], dtype="<f4")
        gaps = []
        for start, stop in intervals:
            part = pcm[int(start * 8000) : int(stop * 8000)]
            if part.size:
                rms = float(np.sqrt(np.mean(np.square(part.astype(np.float64)))))
                db = round(20 * np.log10(max(rms, 1e-9)), 1)
                blocks = [part[i : i + 8000] for i in range(0, part.size, 8000)]
                peak_rms = max(
                    float(np.sqrt(np.mean(np.square(block.astype(np.float64)))))
                    for block in blocks
                )
                gaps.append(
                    {
                        "start": round(start, 3),
                        "end": round(stop, 3),
                        "rms_dbfs": db,
                        "peak_one_second_rms_dbfs": round(
                            20 * np.log10(max(peak_rms, 1e-9)), 1
                        ),
                        "speech_status": "unknown",
                    }
                )
        return {
            "status": "complete",
            "gaps": gaps,
            "note": "RMS includes music, ambient sound and speech; it cannot identify speech.",
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable", "reason": "level_analysis_failed", "gaps": []}
