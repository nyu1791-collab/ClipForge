"Shared helpers."

import json
import wave
import faster_whisper
import numpy as np
import pytest
import math
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from clipforge import media, signals, signals as av, topic_hints as th
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
    monkeypatch.setattr(signals, "model_path", lambda model, revision=None: model)
    settings.threads = 2
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


def test_a_saved_transcript_is_reused_only_when_its_identity_matches(
    tmp_path, monkeypatch, settings
):
    from clipforge import pipeline

    source = tmp_path / "in.mp4"
    source.write_bytes(b"video")
    info = {"audio": True, "duration": 1.0, "audio_start": 0.0}
    calls = []

    def recognise(source, info, settings, journal=None):
        calls.append(settings.threads)
        return {"language": "ja", "segments": []}

    monkeypatch.setattr(signals, "transcribe", recognise)
    settings.threads = 2
    settings.transcript_cache = str(tmp_path / "cache")
    fresh = {}
    first = pipeline.transcribe_once(source, info, settings, fresh)
    reused = {}
    settings.horizontal_target_cps = 3
    assert pipeline.transcribe_once(source, info, settings, reused) == first
    assert fresh["source"] == "asr" and fresh["reused"] is False
    assert reused["source"] == "transcript_cache" and reused["reused"] is True
    assert fresh["key"] == reused["key"]
    assert pipeline.saved_transcript(source, info, settings) == first
    assert calls == [2]
    source.write_bytes(b"different video")
    pipeline.transcribe_once(source, info, settings, reused)
    assert reused["key"] != fresh["key"] and reused["reused"] is False
    assert calls == [2, 2]
    # Threads change the segment boundaries, so the saved transcript must not stand in.
    settings.threads = 4
    pipeline.transcribe_once(source, info, settings)
    assert calls == [2, 2, 4]


def test_a_damaged_saved_transcript_is_recognised_again(
    tmp_path, monkeypatch, settings
):
    from clipforge import pipeline

    source = tmp_path / "in.mp4"
    source.write_bytes(b"video")
    info = {"audio": True, "duration": 1.0, "audio_start": 0.0}
    settings.transcript_cache = str(tmp_path / "cache")
    saved = Path(settings.transcript_cache) / (
        pipeline.transcript_identity(source, info, settings) + ".json"
    )
    saved.parent.mkdir(parents=True)
    saved.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        signals,
        "transcribe",
        lambda source, info, settings, journal=None: {"language": "ja", "segments": []},
    )
    assert pipeline.transcribe_once(source, info, settings) == {
        "language": "ja",
        "segments": [],
    }
    assert (
        Transcript.model_validate_json(saved.read_text(encoding="utf-8")).language
        == "ja"
    )


def test_required_damaged_cache_preserved_without_asr(tmp_path, monkeypatch, settings):
    from clipforge import pipeline
    from clipforge.storage import PipelineError

    source = tmp_path / "in.mp4"
    source.write_bytes(b"video")
    info = {"audio": True, "duration": 1.0}
    settings.transcript_cache = str(tmp_path / "cache")
    settings.require_transcript_cache = True
    saved = Path(settings.transcript_cache) / (
        pipeline.transcript_identity(source, info, settings) + ".json"
    )
    saved.parent.mkdir()
    saved.write_text("{damaged")

    def forbidden(*args):
        pytest.fail("cache-only mode started ASR")

    monkeypatch.setattr(signals, "transcribe", forbidden)
    assert pipeline.saved_transcript(source, info, settings) is None
    with pytest.raises(PipelineError, match="ASR was not started"):
        pipeline.transcribe_once(source, info, settings)
    assert saved.read_text() == "{damaged"


def test_segments_are_journaled_as_recognised_and_progress_is_printed(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(signals, "PROGRESS_SECONDS", 0)
    segments = [
        SimpleNamespace(start=0.0, end=2.0, text="あ", words=[]),
        SimpleNamespace(start=2.0, end=4.0, text="い", words=[]),
    ]
    journal = tmp_path / "x.partial.jsonl"
    seen = list(signals._journaled(iter(segments), 10, journal))
    assert seen == segments  # the segments pass through unchanged
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["text"] for line in lines] == ["あ", "い"]
    assert "ASR progress: 4 of 10 s" in capsys.readouterr().out


def test_a_journal_is_removed_after_a_complete_recognition(
    tmp_path, monkeypatch, settings
):
    from clipforge import pipeline

    source = tmp_path / "in.mp4"
    source.write_bytes(b"video")
    info = {"audio": True, "duration": 1.0, "audio_start": 0.0}
    settings.transcript_cache = str(tmp_path / "cache")
    seen = {}

    def recognise(source, info, settings, journal=None):
        journal.write_text("{}\n")
        seen["journal"] = journal
        return {"language": "ja", "segments": []}

    monkeypatch.setattr(signals, "transcribe", recognise)
    pipeline.transcribe_once(source, info, settings)
    assert not seen["journal"].exists()


def test_transcript_identity_names_gpu_decoding_without_needing_a_gpu(
    monkeypatch, settings
):
    from clipforge import pipeline

    def no_probe(settings):
        raise AssertionError("explicit devices must not probe hardware")

    monkeypatch.setattr(pipeline, "asr_options", no_probe)
    settings.whisper_device, settings.whisper_compute_type = "cuda", "float16"
    gpu = pipeline.asr_identity(settings)
    assert gpu == {"whisper_device": "cuda", "whisper_compute_type": "float16"}
    settings.whisper_device, settings.whisper_compute_type = "cpu", "int8"
    settings.threads = 2
    assert pipeline.asr_identity(settings) == {"threads": 2}
    monkeypatch.setattr(
        pipeline,
        "asr_options",
        lambda s: {"device": "cpu", "compute_type": "int8", "cpu_threads": 2},
    )
    settings.whisper_device = settings.whisper_compute_type = "auto"
    assert pipeline.asr_identity(settings) == {"threads": 2}


HINT_COMMAND = [sys.executable, "-m", "clipforge.topic_hints"]
RATE = 16000


def tone(seconds, amplitude, rate=RATE):
    t = np.arange(int(round(seconds * rate))) / rate
    return amplitude * np.sin(2 * np.pi * 440 * t)


def write_volume_wav(path, samples):
    pcm = np.clip(np.round(samples * 32767), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


def test_full_scale_sine_has_the_expected_rms_level():
    expected = 20 * math.log10(1 / math.sqrt(2))
    assert av.window_db(tone(1, 1.0)) == pytest.approx(expected, abs=1e-3)
    assert av.window_db(tone(0.5, 0.5)) == pytest.approx(expected - 6.0206, abs=1e-3)


def test_silence_and_negligible_noise_are_floored():
    assert av.window_db(np.zeros(RATE)) == av.FLOOR_DB
    assert av.window_db(np.full(RATE, 1e-9)) == av.FLOOR_DB


def test_empty_window_is_rejected():
    with pytest.raises(ValueError):
        av.window_db(np.zeros(0))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_decoded_file_series_matches_synthetic_levels(tmp_path):
    path = tmp_path / "synthetic.wav"
    write_volume_wav(path, np.concatenate([np.zeros(2 * RATE), tone(1, 0.5)]))
    rows = av.loudness_series(path, offset=5.0)
    assert [time for time, _ in rows] == [5.0, 6.0, 7.0]
    assert rows[0][1] == av.FLOOR_DB and rows[1][1] == av.FLOOR_DB
    assert rows[2][1] == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.05)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_undecodable_file_fails_without_echoing_its_path(tmp_path):
    path = tmp_path / "private-name.m4a"
    path.write_text("not audio", encoding="utf-8")
    with pytest.raises(av.VolumeUnavailable, match="could not be measured") as error:
        av.loudness_series(path)
    assert error.value.reason == "decode_failed"
    assert "private-name" not in str(error.value)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_stream_without_samples_reports_no_audio(tmp_path):
    path = tmp_path / "empty.wav"
    write_volume_wav(path, np.zeros(0))
    with pytest.raises(av.VolumeUnavailable) as error:
        av.loudness_series(path)
    assert error.value.reason == "no_audio"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_cli_adds_volume_weight_only_when_audio_is_given(tmp_path):
    audio = tmp_path / "audio.wav"
    write_volume_wav(
        audio, np.concatenate([np.zeros(RATE), tone(1, 0.5), np.zeros(RATE)])
    )
    analysis = {
        "duration": 3,
        "topics": [
            {
                "id": "a",
                "start": 0,
                "end": 3,
                "speech_seconds": 1,
                "text": "通常の話題です。",
            }
        ],
    }
    topics = tmp_path / "topics.json"
    topics.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    plain = subprocess.run([*HINT_COMMAND, str(topics)], capture_output=True, text=True)
    assert plain.returncode == 0
    assert "volume_weight" not in json.loads(plain.stdout)
    with_audio = subprocess.run(
        [*HINT_COMMAND, str(topics), "--audio", str(audio)],
        capture_output=True,
        text=True,
    )
    assert with_audio.returncode == 0
    result = json.loads(with_audio.stdout)
    assert result["volume_weight"] == th.VOLUME_WEIGHT
    assert result["topics"][0]["volume_bump_db"] is not None
    bad = subprocess.run(
        [*HINT_COMMAND, str(topics), "--audio", str(topics)],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1
    assert "invalid input" in bad.stderr
