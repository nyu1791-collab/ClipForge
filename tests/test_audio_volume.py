import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import numpy as np
import pytest

from clipforge import audio_volume as av
from clipforge import topic_hints as th

TOOL = Path(__file__).resolve().parents[1] / "tools" / "topic_hints.py"
RATE = 16000


def tone(seconds, amplitude, rate=RATE):
    t = np.arange(int(round(seconds * rate))) / rate
    return amplitude * np.sin(2 * np.pi * 440 * t)


def write_wav(path, samples):
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
    write_wav(path, np.concatenate([np.zeros(2 * RATE), tone(1, 0.5)]))
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
    write_wav(path, np.zeros(0))
    with pytest.raises(av.VolumeUnavailable) as error:
        av.loudness_series(path)
    assert error.value.reason == "no_audio"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_cli_adds_volume_weight_only_when_audio_is_given(tmp_path):
    audio = tmp_path / "audio.wav"
    write_wav(audio, np.concatenate([np.zeros(RATE), tone(1, 0.5), np.zeros(RATE)]))
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
    plain = subprocess.run(
        [sys.executable, str(TOOL), str(topics)], capture_output=True, text=True
    )
    assert plain.returncode == 0
    assert "volume_weight" not in json.loads(plain.stdout)
    with_audio = subprocess.run(
        [sys.executable, str(TOOL), str(topics), "--audio", str(audio)],
        capture_output=True,
        text=True,
    )
    assert with_audio.returncode == 0
    result = json.loads(with_audio.stdout)
    assert result["volume_weight"] == th.VOLUME_WEIGHT
    assert result["topics"][0]["volume_bump_db"] is not None
    bad = subprocess.run(
        [sys.executable, str(TOOL), str(topics), "--audio", str(topics)],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1
    assert "invalid input" in bad.stderr
