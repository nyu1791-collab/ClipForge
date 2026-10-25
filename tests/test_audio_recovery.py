import hashlib
import shutil
import subprocess

import pytest

from clipforge import media, quality


def report(lufs=-18, peak=-8):
    return {
        "passed": False,
        "failures": ["completed audio loudness is outside -17 to -15 LUFS"],
        "full_decode_performed": True,
        "loudness_measured": True,
        "metrics": {"integrated_lufs": lufs, "true_peak_dbtp": peak},
    }


@pytest.mark.parametrize(
    "lufs,peak", [(-20, -2), (-30, -15), (None, -4), (-float("inf"), -4)]
)
def test_unsafe_or_unmeasured_gain_is_refused(tmp_path, lufs, peak):
    assert media.correct_completed_audio(tmp_path, report(lufs, peak), 3) is None


def test_other_quality_failure_is_not_repaired(tmp_path):
    value = report()
    value["failures"].append("caption text changed")
    assert media.correct_completed_audio(tmp_path, value, 3) is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_completed_aac_gain_preserves_video_and_reaches_target(tmp_path):
    video = tmp_path / "video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=10:d=3",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.17*sin(2*PI*1000*t):s=48000:d=3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(video),
        ],
        check=True,
    )

    def pixels():
        data = subprocess.check_output(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(video),
                "-an",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ]
        )
        return hashlib.sha256(data).hexdigest()

    before = pixels()
    measured = quality.inspect_output(video, True, 3)
    assert measured["integrated_lufs"] < -17
    correction = media.correct_completed_audio(
        tmp_path, report(measured["integrated_lufs"], measured["true_peak_dbtp"]), 3
    )
    assert correction["video_stream_copied"] is True
    assert pixels() == before
    completed = quality.inspect_output(video, True, 3)
    assert -17 <= completed["integrated_lufs"] <= -15
    assert completed["true_peak_dbtp"] <= -1
