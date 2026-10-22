"""Harness contract tests; no model, auth storage, or OpenCode session is used."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("harness", "tools/opencode_harness.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class OpenCodeHarnessTests(unittest.TestCase):
    def test_explicit_model_required_and_stopped_model_rejected(self):
        for model in (
            None,
            "",
            "implicit",
            "opencode/gpt-6.1-sol",
            "other/GPT-6.1-SOL",
        ):
            with self.subTest(model=model), self.assertRaises(ValueError):
                harness.launch_command(
                    "fake-cli",
                    Path("/prepared"),
                    model,
                    "clipforge-review",
                    "synthetic task",
                )

    def test_start_without_model_never_reaches_cli(self):
        with patch.object(harness, "checked") as process, self.assertRaises(SystemExit):
            harness.main(["--start"])
        process.assert_not_called()

    def test_prepare_uses_committed_head_plus_only_harness_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            dest = harness.prepare(parent=d)
            self.assertFalse((dest / "workspace").exists())
            self.assertFalse((dest / ".venv").exists())
            for name in harness.FILES:
                self.assertEqual(
                    (dest / name).read_bytes(), (harness.ROOT / name).read_bytes()
                )
            receipt = json.loads((dest / harness.MARKER).read_text())
            self.assertEqual(receipt["project"], str(dest))
            # Prepared application code must equal HEAD even when the source is dirty.
            committed = harness.checked(
                ["git", "show", "HEAD:src/clipforge/batch.py"], capture_output=True
            ).stdout
            self.assertEqual((dest / "src/clipforge/batch.py").read_bytes(), committed)

    def test_start_sets_all_directory_selectors_without_resuming(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d).resolve()
            for name in harness.FILES:
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((harness.ROOT / name).read_bytes())
            (dest / harness.MARKER).write_text(json.dumps({"project": str(dest)}))
            with (
                patch.object(harness, "cli_path", return_value="fake-cli"),
                patch.object(harness, "checked") as process,
            ):
                harness.main(
                    [
                        "--start",
                        "--project",
                        str(dest),
                        "--model",
                        "test/synthetic",
                        "--task",
                        "synthetic task",
                    ]
                )
            self.assertEqual(process.call_count, 3)
            self.assertEqual(
                process.call_args_list[0].args, process.call_args_list[2].args
            )
            self.assertIn(
                "harness/check_pipeline.py", process.call_args_list[0].args[0]
            )
            args, kwargs = process.call_args_list[1]
            command = args[0]
            self.assertEqual(kwargs["cwd"], dest)
            self.assertEqual(kwargs["env"]["PWD"], str(dest))
            self.assertEqual(
                kwargs["env"]["OPENCODE_CONFIG"], str(dest / "opencode.json")
            )
            self.assertEqual(command[command.index("--dir") + 1], str(dest))
            self.assertTrue({"--session", "--continue", "--attach"}.isdisjoint(command))
            with (
                patch.object(harness, "cli_path", return_value="fake-cli"),
                patch.object(
                    harness,
                    "checked",
                    side_effect=subprocess.CalledProcessError(1, "synthetic"),
                ) as process,
                self.assertRaises(SystemExit),
            ):
                harness.main(
                    [
                        "--start",
                        "--project",
                        str(dest),
                        "--model",
                        "test/synthetic",
                        "--task",
                        "synthetic task",
                    ]
                )
            self.assertEqual(process.call_count, 1)
            self.assertNotIn("fake-cli", process.call_args.args[0])

    def test_default_model_and_external_access_are_rejected(self):
        original = harness.validate(harness.ROOT)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in harness.FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((harness.ROOT / name).read_bytes())
            for mutation in ("model", "permission"):
                config = json.loads(json.dumps(original))
                if mutation == "model":
                    config["agent"]["clipforge"]["model"] = "test/synthetic"
                else:
                    config["agent"]["clipforge"]["permission"]["external_directory"] = (
                        "allow"
                    )
                (root / "opencode.json").write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    harness.validate(root)
