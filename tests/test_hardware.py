"Shared helpers."

import sys
import pytest
import os
import tempfile
import types
import unittest
from types import SimpleNamespace
from clipforge import hardware, pipeline
from pathlib import Path
from unittest.mock import Mock, patch
from clipforge.hardware import model_path
from clipforge.storage import PipelineError


def test_encode_threads_defaults_to_available_cores(monkeypatch, settings):
    monkeypatch.setattr(hardware.os, "sched_getaffinity", lambda pid: set(range(8)))
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 64)
    settings.threads = 2
    assert settings.encode_threads == 0
    assert hardware.encode_threads(settings) == 8
    assert hardware.cpu_threads(settings) == 2


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
    settings.threads = 2
    settings.encode_threads = 6
    assert hardware.encode_threads(settings) == 6
    assert hardware.cpu_threads(settings) == 2


def test_render_threads_leave_asr_options_and_transcript_key_unchanged(
    monkeypatch, tmp_path, settings
):
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(get_supported_compute_types=lambda device: {"int8"}),
    )
    settings.whisper_device, settings.whisper_compute_type = "cpu", "int8"
    settings.threads = 2
    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic video")
    info = {"duration": 10, "audio": True}
    original_key = pipeline.transcript_key(source, info, settings)
    for cores, explicit in [(8, 0), (4, 0), (8, 6)]:
        monkeypatch.setattr(
            hardware.os, "sched_getaffinity", lambda pid, n=cores: set(range(n))
        )
        settings.encode_threads = explicit
        assert hardware.encode_threads(settings) == (explicit or cores)
        assert hardware.asr_options(settings) == {
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 2,
        }
        assert pipeline.asr_identity(settings) == {"threads": 2}
        assert pipeline.transcript_key(source, info, settings) == original_key

    settings.threads = 4
    assert pipeline.transcript_key(source, info, settings) != original_key


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc"])
def test_video_options_use_render_thread_budget(monkeypatch, settings, encoder):
    monkeypatch.setattr(hardware, "nvenc_available", lambda: True)
    settings.video_encoder = encoder
    settings.threads = 2
    settings.encode_threads = 6
    options = hardware.video_options(settings, 18)
    assert options[options.index("-threads") + 1] == "6"


class MissingModel(Exception):
    pass


class ModelCacheTests(unittest.TestCase):
    def modules(self, download):
        utils = types.ModuleType("faster_whisper.utils")
        utils.download_model = download
        errors = types.ModuleType("huggingface_hub.errors")
        errors.LocalEntryNotFoundError = MissingModel
        return {"faster_whisper.utils": utils, "huggingface_hub.errors": errors}

    def test_complete_cache_skips_online_model_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (Path(temporary) / name).write_text("public model asset")
            download = Mock(return_value=temporary)
            with patch.dict(sys.modules, self.modules(download)):
                self.assertEqual(model_path("small", "exact-revision"), temporary)
            download.assert_called_once_with(
                "small", revision="exact-revision", local_files_only=True
            )

    def test_offline_cache_miss_does_not_download_or_change_models(self):
        download = Mock(side_effect=MissingModel())
        with (
            patch.dict(sys.modules, self.modules(download)),
            patch.dict(os.environ, {"CLIPFORGE_OFFLINE": "1"}),
        ):
            with self.assertRaises(PipelineError):
                model_path("small", "exact-revision")
        self.assertEqual(download.call_count, 1)

    def test_online_cache_miss_downloads_only_the_requested_model(self):
        download = Mock(side_effect=[MissingModel(), "/cached/requested-model"])
        with (
            patch.dict(sys.modules, self.modules(download)),
            patch.dict(os.environ, {"CLIPFORGE_OFFLINE": "0", "HF_HUB_OFFLINE": "0"}),
        ):
            self.assertEqual(
                model_path("small", "exact-revision"), "/cached/requested-model"
            )
        self.assertEqual(download.call_count, 2)
        self.assertEqual(
            download.call_args.kwargs,
            {"revision": "exact-revision", "local_files_only": False},
        )
