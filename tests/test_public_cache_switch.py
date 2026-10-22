import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pytest

# Requires the Colab control SDK, which is installed only on the 3.12 CI job.
pytest.importorskip("colab_cli")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import colab_remote_job as remote_job


class PublicCacheSwitchTests(unittest.TestCase):
    """The GPU public cache is neither restored nor published when public_cache is off."""

    def run_prepare_cache(self, public_cache):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "content"
            content.mkdir()
            job = content / "job.json"
            job.write_text(
                json.dumps(
                    {
                        "mode": "prepare-cache",
                        "source_ref": "b" * 40,
                        "workflow_run": "1",
                        "github_token": "",
                        "cache_profile": "gpu",
                        "public_cache": public_cache,
                    }
                )
            )
            report_path = content / "clipforge-report.json"
            real_path = Path

            def fake_path(value, *args):
                if value == "/content/.clipforge-job.json":
                    return job
                if value == "/content/clipforge-report.json":
                    return report_path
                return real_path(value, *args)

            bundles = Mock()
            bundles.restore.return_value = False
            cache_root = root / "cache"
            with (
                patch.object(remote_job, "Path", side_effect=fake_path),
                patch.object(remote_job, "WORK", root / "work"),
                patch.object(remote_job, "ReleaseBundles", return_value=bundles),
                patch.object(
                    remote_job,
                    "configuration",
                    return_value=("k" * 64, cache_root, None),
                ),
                patch.object(remote_job, "release_tag", return_value="tag"),
                patch.object(remote_job, "release_parts", return_value=[]),
                patch.object(remote_job, "command", return_value=None) as command,
                patch.object(remote_job, "host_info", return_value={}),
                patch.object(remote_job, "read_setup_timings", return_value={}),
                patch.object(remote_job, "gpu_info", return_value={}),
            ):
                remote_job.main()
            report = json.loads(report_path.read_text())
            return bundles, command, report

    def test_off_neither_restores_nor_publishes_the_gpu_cache(self):
        bundles, command, report = self.run_prepare_cache("off")
        bundles.restore.assert_not_called()
        bundles.publish.assert_not_called()
        command.assert_called()
        self.assertEqual(report["public_cache_restored"], "skipped_disabled")
        self.assertEqual(report["public_cache_saved"], "skipped_disabled")

    def test_on_restores_and_publishes_the_gpu_cache(self):
        bundles, _, report = self.run_prepare_cache("on")
        bundles.restore.assert_called_once()
        bundles.publish.assert_called_once()
        self.assertIs(report["public_cache_restored"], False)
        self.assertIs(report["public_cache_saved"], True)
