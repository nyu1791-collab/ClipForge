import wave
from types import SimpleNamespace

import faster_whisper
import numpy as np
import pytest

from clipforge import media, signals
from clipforge.models import Transcript
from clipforge.storage import PipelineError


@pytest.fixture(autouse=True)
def fresh_model_cache():
    signals._MODELS.clear()
    yield
    signals._MODELS.clear()


def write_wav(path, samples, rate=16000, channels=1):
    data = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data.tobytes())


def test_samples_are_scaled_from_16_bit_mono_to_unit_range(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path, np.array([0.5, -0.5, 0.0]))
    values = signals.read_samples(path)
    assert values.dtype == np.float32
    assert np.allclose(values, [0.5, -0.5, 0.0], atol=1e-4)


def test_samples_refuse_any_format_but_mono_16k_pcm(tmp_path):
    path = tmp_path / "stereo.wav"
    write_wav(path, np.zeros(8), rate=16000, channels=2)
    with pytest.raises(PipelineError, match="16 kHz"):
        signals.read_samples(path)


def test_transcription_passes_samples_and_keeps_only_spoken_words(
    monkeypatch, settings
):
    monkeypatch.setattr(
        media,
        "audio",
        lambda source, destination, duration: write_wav(destination, np.zeros(16000)),
    )
    seen = {}

    class FakeModel:
        def __init__(self, name, **kwargs):
            seen["name"] = name

        def transcribe(self, audio, **kwargs):
            seen["type"], seen["length"] = type(audio), len(audio)
            words = [
                SimpleNamespace(start=0.2, end=0.6, word=" こんにちは"),
                SimpleNamespace(start=0.6, end=0.9, word="  "),
            ]
            segment = SimpleNamespace(
                start=0.0, end=1.2, text="こんにちは", words=words
            )
            return iter([segment]), SimpleNamespace(language="ja")

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    result = signals.transcribe("/in.mp4", {"audio": True, "duration": 1.0}, settings)
    assert seen["type"] is np.ndarray and seen["length"] == 16000
    transcript = Transcript.model_validate(result)
    assert transcript.language == "ja"
    segment = transcript.segments[0]
    assert segment.end == 1.0  # clipped to the video duration
    assert [word.text for word in segment.words] == [" こんにちは"]


def test_videos_without_audio_have_an_empty_transcript():
    result = signals.transcribe("/in.mp4", {"audio": False, "duration": 5}, None)
    assert result == {"language": "und", "segments": []}


def test_transcript_times_follow_the_audio_stream_start(monkeypatch, settings):
    monkeypatch.setattr(
        media,
        "audio",
        lambda source, destination, duration: write_wav(destination, np.zeros(16000)),
    )

    class FakeModel:
        def __init__(self, name, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            word = SimpleNamespace(start=0.5, end=1.0, word="あ")
            segment = SimpleNamespace(start=0.5, end=1.0, text="あ", words=[word])
            return iter([segment]), SimpleNamespace(language="ja")

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    info = {"audio": True, "duration": 10.0, "audio_start": 2.0}
    transcript = Transcript.model_validate(
        signals.transcribe("/in.mp4", info, settings)
    )
    segment = transcript.segments[0]
    assert (segment.start, segment.end) == (2.5, 3.0)
    assert (segment.words[0].start, segment.words[0].end) == (2.5, 3.0)


def test_batched_decoding_is_used_only_when_requested(monkeypatch, settings):
    monkeypatch.setattr(
        media,
        "audio",
        lambda source, destination, duration: write_wav(destination, np.zeros(16000)),
    )
    used = {}

    class SequentialModel:
        def __init__(self, name, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            used["sequential"] = True
            return iter([]), SimpleNamespace(language="ja")

    class BatchedPipeline:
        def __init__(self, model):
            used["model"] = model

        def transcribe(self, audio, batch_size, **kwargs):
            used["batch_size"] = batch_size
            return iter([]), SimpleNamespace(language="ja")

    monkeypatch.setattr(faster_whisper, "WhisperModel", SequentialModel)
    monkeypatch.setattr(
        faster_whisper, "BatchedInferencePipeline", BatchedPipeline, raising=False
    )
    info = {"audio": True, "duration": 1.0}
    signals.transcribe("/in.mp4", info, settings)
    assert used == {"sequential": True}
    used.clear()
    settings.asr_batch_size = 4
    signals.transcribe("/in.mp4", info, settings)
    assert used["batch_size"] == 4 and "sequential" not in used


def test_one_model_is_loaded_per_configuration(monkeypatch, settings):
    loads = []

    class CountingModel:
        def __init__(self, name, **kwargs):
            loads.append((name, kwargs["cpu_threads"]))

    monkeypatch.setattr(faster_whisper, "WhisperModel", CountingModel)
    first = signals._shared_model(settings)
    assert signals._shared_model(settings) is first
    settings.threads = 4
    assert signals._shared_model(settings) is not first
    assert loads == [(settings.whisper_model, 2), (settings.whisper_model, 4)]


def test_default_model_is_pinned_and_other_models_float_unless_set(settings):
    default = signals.model_revision(settings)
    assert default == signals.PINNED_REVISIONS[settings.whisper_model]
    settings.whisper_model = "small"
    assert signals.model_revision(settings) is None
    settings.whisper_revision = "abc123"
    assert signals.model_revision(settings) == "abc123"
