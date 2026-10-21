"""Silence cutting from speech timing: merge short pauses, pad speech edges, map times.

All times are original-video seconds until mapped with to_output().
"""

import tempfile
from pathlib import Path

from .storage import PipelineError, process


def transcript_spans(transcript, start, end):
    """Speech intervals from word timing (segment timing when words are absent)."""
    spans = []
    for segment in transcript.segments:
        if segment.end <= start or segment.start >= end:
            continue
        for part in segment.words or [segment]:
            a, b = max(start, part.start), min(end, part.end)
            if b > a:
                spans.append((a, b))
    return sorted(spans)


def silero_spans(source, start, end):
    """Optional Silero VAD backend; needs the silero-vad package installed by the user."""
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad, read_audio
    except ImportError as error:
        raise PipelineError(
            "vad=silero needs the optional silero-vad package; use vad=transcript"
        ) from error
    with tempfile.TemporaryDirectory(prefix="clipforge-vad-") as temporary:
        path = Path(temporary) / "clip.wav"
        process(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-n",
                "-ss",
                f"{start:.6f}",
                "-i",
                str(Path(source).resolve()),
                "-t",
                f"{end - start:.6f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(path),
            ],
            timeout=600,
        )
        model = load_silero_vad()
        wave = read_audio(str(path), sampling_rate=16000)
        stamps = get_speech_timestamps(
            wave, model, sampling_rate=16000, return_seconds=True
        )
    spans = []
    for stamp in stamps:
        a, b = max(start, start + stamp["start"]), min(end, start + stamp["end"])
        if b > a:
            spans.append((a, b))
    return sorted(spans)


def keep_regions(spans, start, end, pad, min_gap):
    """Cut only pauses longer than min_gap; keep pad seconds around each speech run."""
    runs = []
    for a, b in spans:
        if runs and a - runs[-1][1] <= min_gap:
            runs[-1][1] = max(runs[-1][1], b)
        else:
            runs.append([a, b])
    regions = []
    for a, b in runs:
        a, b = max(start, a - pad), min(end, b + pad)
        if regions and a <= regions[-1][1]:
            regions[-1][1] = max(regions[-1][1], b)
        else:
            regions.append([a, b])
    return [(a, b) for a, b in regions if b - a >= 0.05]


def output_duration(keep):
    return sum(b - a for a, b in keep)


def to_output(t, keep):
    """Map an original time onto the cut timeline; times inside cuts snap to the cut edge."""
    elapsed = 0.0
    for a, b in keep:
        if t <= a:
            return elapsed
        if t < b:
            return elapsed + (t - a)
        elapsed += b - a
    return elapsed


def pieces(a, b, keep):
    """Output-time pieces of the original interval [a, b] that survive the cut."""
    result = []
    elapsed = 0.0
    for x, y in keep:
        lo, hi = max(a, x), min(b, y)
        if hi > lo:
            result.append((elapsed + (lo - x), elapsed + (hi - x), len(result)))
        elapsed += y - x
    return result
