"""Transcription and optional chat signals share original-video seconds."""

import json
from pathlib import Path
import tempfile
import threading
import time
import wave

from . import media
from .hardware import asr_options
from .models import Segment, Transcript, Word
from .storage import PipelineError
from .whisper_cache import model_path


def read_samples(path):
    """Mono 16 kHz 16-bit WAV as float32 samples in [-1, 1)."""
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
            1,
            2,
            media.ASR_SAMPLE_RATE,
        ):
            raise PipelineError("speech audio must be mono 16-bit PCM at 16 kHz")
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


_MODELS = {}
_MODELS_LOCK = threading.Lock()
# One decoder at a time. A loaded model is not assumed to be safe for concurrent calls,
# so batched videos share the model and take turns in the decoder only.
_DECODE_LOCK = threading.Lock()
# The revision the default model was tested with. A repository update cannot change the
# transcript of an existing job, because the same weights are fetched every time.
PINNED_REVISIONS = {
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
}


def model_revision(settings):
    """Explicit revision, else the pinned one for a known model; None floats with the hub."""
    return settings.whisper_revision or PINNED_REVISIONS.get(settings.whisper_model)


def _shared_model(settings):
    """One loaded Whisper model per configuration, reused by every video in the process."""
    from faster_whisper import WhisperModel

    revision = model_revision(settings)
    # CUDA/float16 when a GPU is usable (or required), else CPU/int8 on every available core.
    options = asr_options(settings)
    key = (settings.whisper_model, revision, tuple(sorted(options.items())))
    with _MODELS_LOCK:
        if key not in _MODELS:
            print(
                f"ASR: {options['device']}/{options['compute_type']}, "
                f"CPU threads={options['cpu_threads']}",
                flush=True,
            )
            _MODELS[key] = WhisperModel(
                model_path(settings.whisper_model, revision),
                **options,
                num_workers=1,
            )
        return _MODELS[key]


def _utterances(segments, info):
    """Segments in the file's own clock, with words kept inside their segment."""
    output = []
    # The extracted WAV starts where the audio stream starts; shift back to the file's clock.
    offset = float(info.get("audio_start", 0.0))
    for segment in segments:
        start = max(0, segment.start + offset)
        end = min(info["duration"], segment.end + offset)
        if end <= start:
            continue
        words = []
        for word in segment.words or []:
            ws = max(start, word.start + offset)
            we = min(end, word.end + offset)
            if we > ws and word.word.strip():
                words.append(Word(start=ws, end=we, text=word.word))
        output.append(Segment(start=start, end=end, text=segment.text, words=words))
    return output


PROGRESS_SECONDS = 30


def _journaled(segments, duration, journal):
    """Yield recognised segments unchanged, noting each one and printing progress.

    Recognition is one continuous pass, so the result does not depend on this. The journal keeps
    what was recognised if the run is stopped (a timeout, a cancel), and the progress line says
    how far the audio has been read. Journal lines hold speech text: keep them private.
    """
    last_report = time.monotonic()
    with journal.open("a", encoding="utf-8") if journal else _no_journal() as out:
        for segment in segments:
            yield segment
            if out is not None:
                out.write(
                    json.dumps(
                        {
                            "start": segment.start,
                            "end": segment.end,
                            "text": segment.text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                out.flush()
            if time.monotonic() - last_report >= PROGRESS_SECONDS:
                last_report = time.monotonic()
                print(
                    f"ASR progress: {segment.end:.0f} of {duration:.0f} s", flush=True
                )


class _no_journal:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def transcribe(source, info, settings, journal=None):
    if not info["audio"]:
        return Transcript(language="und", segments=[]).model_dump()
    if settings.asr_batch_size and settings.whisper_device == "cuda":
        # Checked again here: model_copy does not run the settings validator.
        raise PipelineError(
            "batched ASR is CPU-only; GPU recognition runs sequentially"
        )
    with tempfile.TemporaryDirectory(prefix="clipforge-asr-") as temporary:
        path = Path(temporary) / "audio.wav"
        media.audio(source, path, info["duration"])
        # Samples, not a path: faster-whisper would otherwise decode with PyAV, whose
        # open() arguments changed in PyAV 19 (the locked build for Python 3.12+).
        audio = read_samples(path)
        model = _shared_model(settings)
        options = dict(
            language=settings.language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        with _DECODE_LOCK:
            if settings.asr_batch_size:
                # Batched decoding is about twice as fast on CPU, but it merges silences
                # differently, which changes topic boundaries; it is opt-in for that reason.
                from faster_whisper import BatchedInferencePipeline

                pipeline = BatchedInferencePipeline(model=model)
                segments, metadata = pipeline.transcribe(
                    audio, batch_size=settings.asr_batch_size, **options
                )
            else:
                segments, metadata = model.transcribe(audio, **options)
            output = _utterances(_journaled(segments, info["duration"], journal), info)
        return Transcript(
            language=metadata.language or "und", segments=output
        ).model_dump()
