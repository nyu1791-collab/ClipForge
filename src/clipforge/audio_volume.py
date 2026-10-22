"""Per-second loudness of an audio file, used only as a review hint.

Levels are RMS values in dB relative to full scale, one per second on the topic
clock. The audio is decoded to mono 16 kHz PCM in fixed one-second blocks, so
memory does not grow with the length of the stream. No text is produced.
"""

import math
import subprocess

from .media import ASR_SAMPLE_RATE
from .storage import PipelineError
from .topic_hints import VOLUME_WINDOW_SECONDS

FLOOR_DB = -90.0


class VolumeUnavailable(PipelineError):
    """No loudness series. ``reason`` is a fixed code, never a path or ffmpeg output.

    "decode_failed": ffmpeg could not read the first audio stream.
    "no_audio": the stream decoded to no samples.
    """

    def __init__(self, reason):
        super().__init__("audio volume could not be measured")
        self.reason = reason


_BYTES_PER_WINDOW = ASR_SAMPLE_RATE * 2  # mono, 16-bit PCM, one second


def window_db(samples):
    """RMS level in dB of float samples in [-1, 1), never below FLOOR_DB."""
    import numpy as np

    samples = np.asarray(samples, dtype=np.float64)
    if samples.size == 0:
        raise ValueError("an empty window has no level")
    rms = float(np.sqrt(np.mean(np.square(samples))))
    if rms <= 0.0:
        return FLOOR_DB
    return max(FLOOR_DB, 20.0 * math.log10(rms))


def loudness_series(path, offset=0.0):
    """(time, dB) for each second of the first audio stream in ``path``.

    ``offset`` is the topic-clock time of the first sample (the stream start).
    Failures raise VolumeUnavailable with a fixed reason code.
    """
    import numpy as np

    command = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(ASR_SAMPLE_RATE),
        "-f",
        "s16le",
        "-",
    ]
    rows = []
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(_BYTES_PER_WINDOW)
            usable = len(chunk) - len(chunk) % 2
            if usable == 0:
                break
            samples = np.frombuffer(chunk[:usable], dtype="<i2")
            samples = samples.astype(np.float64) / 32768.0
            rows.append(
                (offset + len(rows) * VOLUME_WINDOW_SECONDS, window_db(samples))
            )
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    code = proc.wait()
    if code:
        raise VolumeUnavailable("decode_failed")
    if not rows:
        raise VolumeUnavailable("no_audio")
    return rows
