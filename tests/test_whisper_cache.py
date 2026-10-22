import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clipforge.whisper_cache import model_path
from clipforge.storage import PipelineError


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


if __name__ == "__main__":
    unittest.main()
