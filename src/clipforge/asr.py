"""Japanese speech recognition with NVIDIA Parakeet on a Colab GPU.

This module runs in the Colab runtime's own Python, which ships PyTorch with CUDA, so the
locked project environment is never installed there. It needs only the standard library,
numpy and NeMo. ``prefetch`` installs NeMo and downloads the model while the input audio is
still downloading; ``recognise`` waits for it under the same lock.

Audio is recognised in pieces of about eight minutes cut at the quietest moment near each
boundary, so no word is split and full attention fits a T4.
"""

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

MODEL = "nvidia/parakeet-tdt_ctc-0.6b-ja"
NEMO = "nemo_toolkit[asr]==3.0.0"
SAMPLE_RATE = 16000
PIECE_SECONDS = 480
# A cut is placed at the quietest WINDOW_SECONDS within SEARCH_SECONDS of each boundary.
SEARCH_SECONDS = 30
WINDOW_SECONDS = 0.2
# Characters closer than this are one caption run; a longer run is cut at its widest pause.
PAUSE_SECONDS = 0.35
LONGEST_CHARS = 30
# A zero-length character is given this much time, never past the next character.
MIN_CHAR_SECONDS = 0.08
LOCK = Path(tempfile.gettempdir()) / "clipforge-nemo.lock"


def read_samples(path):
    """Mono 16 kHz 16-bit WAV as float32 samples in [-1, 1)."""
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        check_format(handle)
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def check_format(handle):
    if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (
        1,
        2,
        SAMPLE_RATE,
    ):
        raise ValueError("speech audio must be mono 16-bit PCM at 16 kHz")


def write_samples(path, samples):
    import numpy as np

    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def quietest(samples, rate=SAMPLE_RATE):
    """Offset of the centre of the quietest WINDOW_SECONDS in samples."""
    import numpy as np

    window = min(int(WINDOW_SECONDS * rate), len(samples))
    # Sums of every window, from one cumulative sum of the energy.
    total = np.concatenate(
        ([0.0], np.cumsum(np.asarray(samples, dtype=np.float64) ** 2))
    )
    return int(np.argmin(total[window:] - total[:-window])) + window // 2


def cut_points(samples, rate=SAMPLE_RATE):
    """Sample offsets that split the audio into pieces at quiet moments."""
    cuts, position = [0], 0
    while len(samples) - position > (PIECE_SECONDS + SEARCH_SECONDS) * rate:
        lo = position + (PIECE_SECONDS - SEARCH_SECONDS) * rate
        hi = position + (PIECE_SECONDS + SEARCH_SECONDS) * rate
        position = lo + quietest(samples[lo:hi], rate)
        cuts.append(position)
    cuts.append(len(samples))
    return cuts


def _text(unit):
    value = unit.get("char")
    return "".join(value) if isinstance(value, list) else (value or "")


def _runs(chars):
    groups, current = [], []
    for char in chars:
        if current and char[1] - current[-1][2] >= PAUSE_SECONDS:
            groups.append(current)
            current = []
        current.append(char)
    if current:
        groups.append(current)
    runs = []
    while groups:
        group = groups.pop(0)
        if len(group) <= LONGEST_CHARS:
            runs.append(group)
            continue
        cut = max(range(4, len(group) - 4), key=lambda i: group[i][1] - group[i - 1][2])
        groups[:0] = [group[:cut], group[cut:]]
    return runs


def segments(chars, start, end):
    """Transcript-shaped segments in source seconds for one piece's character timestamps.

    ``chars`` are Parakeet's character stamps in piece seconds; the piece spans start..end of
    the source. Characters are grouped into runs at pauses; each character is one word.
    """
    stamps = [
        (_text(c), float(c["start"]), float(c["end"]))
        for c in chars
        if _text(c).strip()
    ]
    out = []
    for members in _runs(stamps):
        words = []
        for index, (text, a, b) in enumerate(members):
            following = members[index + 1][1] if index + 1 < len(members) else b + 0.3
            first = start + a
            last = start + max(b, min(following, a + MIN_CHAR_SECONDS))
            if words and first < words[-1]["end"]:
                first = words[-1]["end"]
            last = max(last, first + 0.01)
            if last > end:
                break
            words.append(
                {"text": text, "start": round(first, 2), "end": round(last, 2)}
            )
        words = [w for w in words if w["end"] > w["start"]]
        if words:
            out.append(
                {
                    "start": words[0]["start"],
                    "end": words[-1]["end"],
                    "text": "".join(w["text"] for w in words),
                    "words": words,
                }
            )
    return out


def prepare(load=True):
    """Install NeMo into this Python and fetch the model files once, under one lock.

    With load, the model is also loaded; the files come from the Hugging Face cache that
    the prefetch filled.
    """
    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if importlib.util.find_spec("nemo") is None:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", NEMO], check=True
            )
        from huggingface_hub import snapshot_download

        snapshot_download(MODEL)
    if load:
        import nemo.collections.asr as nemo_asr

        return nemo_asr.models.ASRModel.from_pretrained(MODEL)
    return None


def recognise(source, output, language="ja"):
    """Write the transcript of a 16 kHz mono WAV as JSON segments in its own seconds."""
    samples = read_samples(source)
    cuts = cut_points(samples)
    with tempfile.TemporaryDirectory(prefix="clipforge-asr-") as temporary:
        pieces = []
        for index, (a, b) in enumerate(zip(cuts, cuts[1:])):
            path = Path(temporary) / f"piece-{index:03}.wav"
            write_samples(path, samples[a:b])
            pieces.append((str(path), a / SAMPLE_RATE, b / SAMPLE_RATE))
        model = prepare().cuda().eval()
        results = model.transcribe(
            [path for path, _, _ in pieces], timestamps=True, batch_size=1
        )
    if len(results) != len(pieces):
        raise RuntimeError("Parakeet returned a different number of pieces")
    rows = []
    for result, (_, start, end) in zip(results, pieces):
        stamps = getattr(result, "timestamp", None) or {}
        rows += segments(stamps.get("char") or [], start, end)
    duration = len(samples) / SAMPLE_RATE
    value = {
        "language": language,
        "segments": [s for s in rows if s["end"] <= duration + 0.01],
    }
    Path(output).write_text(json.dumps(value, ensure_ascii=False))


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prefetch")
    run = sub.add_parser("recognise")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--language", default="ja")
    args = parser.parse_args(argv)
    if args.command == "prefetch":
        prepare(load=False)
    else:
        recognise(args.input, args.output, args.language)


if __name__ == "__main__":
    main()
