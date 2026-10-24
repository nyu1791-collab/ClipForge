"""Recognise speech with NVIDIA Parakeet in a separate interpreter; return a Transcript.

NeMo needs its own PyTorch, so it runs in the interpreter named by
CLIPFORGE_PARAKEET_PYTHON (the Colab system Python, which ships PyTorch with CUDA),
never in the locked environment. Long audio is recognised in pieces of about eight
minutes cut at the quietest moment near each boundary, so no word is split and full
attention fits a T4.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

from . import media, parakeet_captions, signals
from .models import Transcript
from .storage import PipelineError

MODEL = "nvidia/parakeet-tdt_ctc-0.6b-ja"
NEMO = "nemo_toolkit[asr]==3.0.0"
PIECE_SECONDS = 480
# A cut is placed at the quietest 0.2 s within this many seconds of each boundary.
SEARCH_SECONDS = 30
WINDOW_SECONDS = 0.2

RUNNER = r"""
import importlib.util, json, subprocess, sys
if importlib.util.find_spec("nemo") is None:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", sys.argv[3]], check=True)
import nemo.collections.asr as nemo_asr
model = nemo_asr.models.ASRModel.from_pretrained(sys.argv[4]).cuda().eval()
paths = json.load(open(sys.argv[1]))
outputs = model.transcribe(paths, timestamps=True, batch_size=1)
rows = []
for out in outputs:
    stamps = getattr(out, "timestamp", None) or {}
    rows.append({"char": stamps.get("char") or []})
json.dump(rows, open(sys.argv[2], "w"), ensure_ascii=False, default=str)
"""


def cut_points(samples, rate=media.ASR_SAMPLE_RATE):
    """Sample offsets that split the audio into pieces at quiet moments."""
    import numpy as np

    window = int(WINDOW_SECONDS * rate)
    cuts, position = [0], 0
    while len(samples) - position > (PIECE_SECONDS + SEARCH_SECONDS) * rate:
        lo = position + (PIECE_SECONDS - SEARCH_SECONDS) * rate
        hi = position + (PIECE_SECONDS + SEARCH_SECONDS) * rate
        region = samples[lo:hi] ** 2
        energy = np.convolve(region, np.ones(window), mode="valid")
        position = lo + int(np.argmin(energy)) + window // 2
        cuts.append(position)
    cuts.append(len(samples))
    return cuts


def _write(path, samples, rate=media.ASR_SAMPLE_RATE):
    import numpy as np

    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


def interpreter():
    named = os.environ.get("CLIPFORGE_PARAKEET_PYTHON")
    if named:
        return named
    return "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable


def transcribe(source, info, settings):
    if not info["audio"]:
        return Transcript(language="und", segments=[]).model_dump()
    rate = media.ASR_SAMPLE_RATE
    offset = float(info.get("audio_start", 0.0))
    with tempfile.TemporaryDirectory(prefix="clipforge-parakeet-") as temporary:
        root = Path(temporary)
        media.audio(source, root / "audio.wav", info["duration"])
        samples = signals.read_samples(root / "audio.wav")
        cuts = cut_points(samples)
        pieces = []
        for index, (a, b) in enumerate(zip(cuts, cuts[1:])):
            path = root / f"piece-{index:03}.wav"
            _write(path, samples[a:b])
            pieces.append((str(path), a / rate, b / rate))
        listing = root / "pieces.json"
        listing.write_text(json.dumps([p for p, _, _ in pieces]))
        result = root / "result.json"
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("VIRTUAL_ENV", "PYTHON"))
        }
        environment["PATH"] = os.pathsep.join(
            p for p in environment.get("PATH", "").split(os.pathsep) if ".venv" not in p
        )
        completed = subprocess.run(
            [interpreter(), "-c", RUNNER, str(listing), str(result), NEMO, MODEL],
            env=environment,
            timeout=max(1800, info["duration"]),
            check=False,
        )
        if completed.returncode != 0 or not result.is_file():
            raise PipelineError("Parakeet recognition failed")
        rows = json.loads(result.read_text())
    if len(rows) != len(pieces):
        raise PipelineError("Parakeet returned a different number of pieces")
    segments = []
    for row, (_, start, end) in zip(rows, pieces):
        segments += parakeet_captions.segments(row, offset + start, offset + end)
    duration = float(info["duration"])
    segments = [s for s in segments if s["end"] <= duration + 0.01]
    return Transcript(language=settings.language, segments=segments).model_dump()
