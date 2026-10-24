import pytest

from clipforge import hardware


def test_encode_threads_defaults_to_available_cores(monkeypatch, settings):
    monkeypatch.setattr(hardware.os, "sched_getaffinity", lambda pid: set(range(8)))
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 64)
    assert settings.encode_threads == 0
    assert hardware.encode_threads(settings) == 8


@pytest.mark.parametrize("unavailable", ["missing", "error"])
@pytest.mark.parametrize("cores,expected", [(8, 8), (None, 1), (0, 1)])
def test_encode_threads_falls_back_to_cpu_count(
    monkeypatch, settings, unavailable, cores, expected
):
    def failed_affinity(pid):
        raise OSError("affinity unavailable")

    if unavailable == "missing":
        monkeypatch.delattr(hardware.os, "sched_getaffinity", raising=False)
    else:
        monkeypatch.setattr(hardware.os, "sched_getaffinity", failed_affinity)
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: cores)
    assert hardware.encode_threads(settings) == expected


def test_explicit_encode_threads_takes_precedence(monkeypatch, settings):
    def forbidden(*args):
        pytest.fail("explicit thread counts must not probe hardware")

    monkeypatch.setattr(hardware.os, "sched_getaffinity", forbidden)
    monkeypatch.setattr(hardware.os, "cpu_count", forbidden)
    settings.encode_threads = 6
    assert hardware.encode_threads(settings) == 6


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc"])
def test_video_options_use_render_thread_budget(monkeypatch, settings, encoder):
    monkeypatch.setattr(hardware, "nvenc_available", lambda: True)
    settings.video_encoder = encoder
    settings.encode_threads = 6
    options = hardware.video_options(settings, 18)
    assert options[options.index("-threads") + 1] == "6"
