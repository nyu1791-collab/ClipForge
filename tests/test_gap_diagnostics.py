import numpy as np
from types import SimpleNamespace

from clipforge import gap_diagnostics


def test_levels_are_advisory_and_never_claim_speech(monkeypatch):
    samples = np.full(8000 * 20, 0.25, dtype="<f4").tobytes()
    monkeypatch.setattr(
        gap_diagnostics.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=samples),
    )
    result = gap_diagnostics.describe_gaps(
        "synthetic.ogg", 20, [{"start": 15, "end": 16}]
    )
    assert result["status"] == "complete"
    assert result["gaps"][0]["start"] == 0
    assert result["gaps"][0]["end"] == 15
    assert result["gaps"][0]["speech_status"] == "unknown"
    assert -13 < result["gaps"][0]["rms_dbfs"] < -11


def test_decode_failure_is_nonfatal(monkeypatch):
    monkeypatch.setattr(
        gap_diagnostics.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=b""),
    )
    assert gap_diagnostics.describe_gaps("bad", 15, [])["status"] == "unavailable"
