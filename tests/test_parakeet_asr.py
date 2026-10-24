import json

import numpy as np
import pytest

from clipforge import parakeet_asr, pipeline
from clipforge.models import Settings

RATE = 16000


def test_pieces_are_cut_at_the_quietest_moment_near_each_boundary(monkeypatch):
    monkeypatch.setattr(parakeet_asr, "PIECE_SECONDS", 10)
    monkeypatch.setattr(parakeet_asr, "SEARCH_SECONDS", 2)
    samples = np.full(25 * RATE, 0.5, dtype=np.float32)
    samples[int(10.7 * RATE) : int(11.1 * RATE)] = 0.0
    cuts = parakeet_asr.cut_points(samples)
    assert cuts[0] == 0 and cuts[-1] == len(samples)
    assert 10.7 * RATE <= cuts[1] <= 11.1 * RATE
    assert all(b - a <= 12 * RATE for a, b in zip(cuts, cuts[1:]))


def test_short_audio_is_one_piece():
    samples = np.zeros(60 * RATE, dtype=np.float32)
    assert parakeet_asr.cut_points(samples) == [0, len(samples)]


def test_pieces_return_to_the_source_clock(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(parakeet_asr, "PIECE_SECONDS", 10)
    monkeypatch.setattr(parakeet_asr, "SEARCH_SECONDS", 2)
    monkeypatch.setattr(
        parakeet_asr.media,
        "audio",
        lambda source, path, duration: parakeet_asr._write(
            path, np.zeros(25 * RATE, dtype=np.float32)
        ),
    )

    def run(command, **kwargs):
        pieces = json.loads(open(command[3]).read())
        rows = [{"char": [{"char": ["語"], "start": 1.0, "end": 1.2}]} for _ in pieces]
        open(command[4], "w").write(json.dumps(rows))
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(parakeet_asr.subprocess, "run", run)
    value = parakeet_asr.transcribe(
        tmp_path / "in.mp4",
        {"audio": True, "duration": 25.0, "audio_start": 0.5},
        settings.model_copy(update={"language": "ja"}),
    )
    cuts = parakeet_asr.cut_points(np.zeros(25 * RATE, dtype=np.float32))
    starts = [s["start"] for s in value["segments"]]
    assert starts == pytest.approx([0.5 + c / RATE + 1.0 for c in cuts[:-1]], abs=0.01)


def test_failed_recognition_is_reported(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(
        parakeet_asr.media,
        "audio",
        lambda source, path, duration: parakeet_asr._write(
            path, np.zeros(RATE, dtype=np.float32)
        ),
    )
    monkeypatch.setattr(
        parakeet_asr.subprocess,
        "run",
        lambda *a, **k: type("Done", (), {"returncode": 1})(),
    )
    with pytest.raises(parakeet_asr.PipelineError, match="Parakeet"):
        parakeet_asr.transcribe(
            tmp_path / "in.mp4", {"audio": True, "duration": 1.0}, settings
        )


def test_only_parakeet_changes_the_transcript_identity(tmp_path, settings):
    source = tmp_path / "in.mp4"
    source.write_bytes(b"x")
    info = {"duration": 1.0, "audio": True}
    whisper = pipeline.transcript_identity(source, info, settings)
    assert whisper == pipeline.transcript_identity(
        source, info, settings.model_copy(update={"asr_engine": "whisper"})
    )
    parakeet = settings.model_copy(update={"asr_engine": "parakeet"})
    assert pipeline.transcript_identity(source, info, parakeet) != whisper


@pytest.mark.parametrize("update", [{"asr_parts": 2}, {"asr_batch_size": 8}])
def test_parakeet_is_one_unbatched_part(settings, update):
    with pytest.raises(ValueError, match="Parakeet"):
        Settings.model_validate(
            {**settings.model_dump(), "asr_engine": "parakeet", **update}
        )
