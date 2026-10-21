import json
import subprocess

import pytest

from clipforge import media
from clipforge.models import SelectedClip
from clipforge.storage import PipelineError


@pytest.mark.parametrize(
    "data",
    [
        {"streams": [], "format": {"duration": "10"}},
        {"streams": [{"codec_type": "audio"}], "format": {"duration": "10"}},
        {
            "streams": [{"codec_type": "video", "width": 320, "height": 180}],
            "format": {"duration": "nan"},
        },
        {
            "streams": [{"codec_type": "video", "width": 320, "height": 180}],
            "format": {"duration": "0"},
        },
        {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 320,
                    "height": 180,
                    "disposition": {"attached_pic": 1},
                }
            ],
            "format": {"duration": "10"},
        },
    ],
)
def test_probe_rejects_invalid_video(monkeypatch, data, tmp_path):
    monkeypatch.setattr(
        media,
        "process",
        lambda command: subprocess.CompletedProcess(
            command, 0, json.dumps(data).encode(), b""
        ),
    )
    with pytest.raises(PipelineError):
        media.probe(tmp_path / "input")


def test_probe_whitelists_protocols_and_ignores_cover_art(monkeypatch, tmp_path):
    def execute(command):
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert "hls" not in command[command.index("-format_whitelist") + 1]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "format": {"duration": "10.25"},
                    "streams": [
                        {
                            "codec_type": "video",
                            "width": 20,
                            "height": 20,
                            "disposition": {"attached_pic": 1},
                        },
                        {"codec_type": "video", "width": 320, "height": 180},
                        {"codec_type": "audio"},
                    ],
                }
            ).encode(),
            b"",
        )

    monkeypatch.setattr(media, "process", execute)
    assert media.probe(tmp_path / "input") == {
        "duration": 10.25,
        "width": 320,
        "height": 180,
        "audio": True,
        "audio_start": 0.0,
        "frame_rate": None,
    }


def test_probe_reports_the_exact_frame_rate(monkeypatch, tmp_path):
    stream = {
        "codec_type": "video",
        "width": 640,
        "height": 360,
        "avg_frame_rate": "30000/1001",
        "r_frame_rate": "30000/1001",
    }
    output = {"format": {"duration": "5"}, "streams": [stream]}
    monkeypatch.setattr(
        media,
        "process",
        lambda command: subprocess.CompletedProcess(
            command, 0, json.dumps(output).encode(), b""
        ),
    )
    assert media.probe(tmp_path / "input")["frame_rate"] == "30000/1001"
    # When the average is missing, the nominal rate is used; a zero rate is unusable.
    stream.update(avg_frame_rate="0/0", r_frame_rate="25/1")
    assert media.probe(tmp_path / "input")["frame_rate"] == "25"
    stream.update(avg_frame_rate="0/0", r_frame_rate="0/0")
    assert media.probe(tmp_path / "input")["frame_rate"] is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("25", "25"),
        ("30000/1001", "30000/1001"),
        ("30", "30"),
        ("60", "30"),
        ("120", "30"),
        (None, "30"),
    ],
)
def test_output_keeps_slow_sources_and_lowers_fast_ones_to_30(source, expected):
    # A 25 fps source must not be repeated up to 30 fps; faster sources drop frames.
    assert str(media.output_rate(source)) == expected


LOUDNESS = (
    '{"input_i": "-20.00", "input_tp": "-3.00", "input_lra": "5.00", '
    '"input_thresh": "-30.00", "target_offset": "0.50"}'
)


def fake_ffmpeg(render_check):
    """Stand-in for subprocess.run: the loudness pass gets its measurement, the render is checked."""

    def execute(command, **kwargs):
        if command[-3:] == ["-f", "null", "-"]:
            return subprocess.CompletedProcess(command, 0, "", LOUDNESS)
        return render_check(command, kwargs)

    return execute


@pytest.mark.parametrize("audio", [False, True])
@pytest.mark.parametrize("framing", ["fit", "center-crop"])
def test_render_commands_are_local_relative_and_cpu(
    monkeypatch, tmp_path, settings, audio, framing
):
    settings.framing = framing
    settings.layout = "fit"  # the framing choice only applies to the fit layout
    settings.height = 720
    clip = SelectedClip(
        start=21, end=27, candidate_id="manual", title="t", reason="r", confidence=1
    )
    destination = tmp_path / "video.mp4"
    info = {"duration": 6, "width": 404, "height": 720, "audio": audio}
    monkeypatch.setattr(media, "probe", lambda path: info)

    def check(command, kwargs):
        assert "-n" in command
        assert command[command.index("-ss") + 1] == "21.000000"
        assert command[command.index("-t") + 1] == "6.000000"
        assert command[command.index("-map") + 1] == "[vout]"
        assert ("[aout]" in command) == audio
        assert command[command.index("-c:v") + 1] == "libx264"
        assert command[command.index("-preset") + 1] == settings.encode_preset == "slow"
        assert command[command.index("-crf") + 1] == str(media.VIDEO_CRF) == "18"
        graph = command[command.index("-filter_complex") + 1]
        assert "setpts=PTS-STARTPTS" in graph
        assert "ass=filename=captions.ass" in graph
        assert ("pad=" in graph) == (framing == "fit")
        assert ("crop=" in graph) == (framing == "center-crop")
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg(check))
    assert (
        media.render(
            tmp_path / "source with spaces.mp4",
            destination,
            clip,
            settings,
            ass=tmp_path / "captions.ass",
        )
        == info
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"duration": 1, "width": 404, "height": 720, "audio": True},
        {"duration": 6, "width": 500, "height": 720, "audio": True},
    ],
)
def test_render_rejects_incomplete_output(monkeypatch, tmp_path, settings, bad):
    monkeypatch.setattr(media, "probe", lambda path: bad)
    monkeypatch.setattr(
        subprocess,
        "run",
        fake_ffmpeg(
            lambda command, kwargs: subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    clip = SelectedClip(
        start=0, end=6, candidate_id="manual", title="t", reason="r", confidence=1
    )
    with pytest.raises(PipelineError):
        media.render(tmp_path / "input", tmp_path / "video.mp4", clip, settings)


def test_decoding_errors_rejected_without_echo(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media,
        "process",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 0, b"", b"private decoder detail"
        ),
    )
    with pytest.raises(PipelineError) as caught:
        media.audio(tmp_path / "input", tmp_path / "audio.wav", 30)
    assert "private decoder detail" not in str(caught.value)
