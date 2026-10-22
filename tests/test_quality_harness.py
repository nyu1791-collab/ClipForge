"""Exercise harness failure gates with synthetic captions and no external APIs."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pysubs2

from harness import check_pipeline, lint_captions


class QualityHarnessTests(unittest.TestCase):
    def pair(self, directory, changes=None):
        values = {"start": 0, "end": 1000, "text": "合成の字幕"}
        values.update(changes or {})
        ass = pysubs2.SSAFile()
        ass.append(pysubs2.SSAEvent(**values))
        ass.save(str(directory / "captions.ass"))
        ass.save(str(directory / "captions.srt"))
        (directory / "caption_review.json").write_text(
            json.dumps({"events": 1, "warnings": [], "missing_glyphs": []})
        )

    def test_caption_pair_and_cli_accept_valid_local_data(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.pair(root)
            self.assertEqual(lint_captions.check(root), {"events": 1, "warnings": 0})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lint_captions.main([d]), 0)

    def test_caption_bounds_overlap_empty_and_format_mismatch_fail(self):
        for case in ("empty", "late", "lines", "overlap", "mismatch", "none"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                changes = {
                    "empty": {"text": " "},
                    "late": {"end": 4010},
                    "lines": {"text": r"一\N二\N三"},
                }
                self.pair(root, changes.get(case))
                if case in ("overlap", "none"):
                    for name in ("captions.ass", "captions.srt"):
                        subs = pysubs2.load(str(root / name))
                        if case == "overlap":
                            subs.append(
                                pysubs2.SSAEvent(start=900, end=1500, text="重複")
                            )
                        else:
                            subs.clear()
                        subs.save(str(root / name))
                elif case == "mismatch":
                    (root / "captions.srt").write_text(
                        "1\n00:00:00,000 --> 00:00:01,000\n相違\n"
                    )
                with self.assertRaises(ValueError):
                    lint_captions.check(root)

    def test_caption_review_warning_count_glyph_and_cli_failure_gates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.pair(root)
            for update in (
                {"warnings": ["synthetic"]},
                {"warnings": "invalid"},
                {"events": 2},
                {"missing_glyphs": ["合成"]},
            ):
                review = {"events": 1, "warnings": [], "missing_glyphs": []}
                review.update(update)
                (root / "caption_review.json").write_text(json.dumps(review))
                with self.subTest(update=update), self.assertRaises(ValueError):
                    lint_captions.check(root)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(lint_captions.main([d]), 1)
            (root / "caption_review.json").write_text(
                json.dumps(
                    {"events": 1, "warnings": ["synthetic"], "missing_glyphs": []}
                )
            )
            self.assertEqual(lint_captions.check(root, max_warnings=1)["warnings"], 1)
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                lint_captions.main([d, "--max-warnings", "-1"])

    def test_contracts_reject_out_of_date_schema(self):
        check_pipeline.check_contracts()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in check_pipeline.validate.__globals__["FILES"]:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(check_pipeline.ROOT / name, target)
            (root / "schemas").mkdir()
            (root / "schemas/clips_manifest.schema.json").write_text("{}")
            with self.assertRaises(ValueError):
                check_pipeline.check_contracts(root)

    def test_runner_stops_at_first_failure_or_timeout_without_shell(self):
        failures = (
            subprocess.CompletedProcess([], 1),
            OSError("synthetic"),
            subprocess.TimeoutExpired([], 300),
        )
        for failure in failures:
            with (
                self.subTest(failure=type(failure).__name__),
                contextlib.redirect_stdout(io.StringIO()),
                patch.object(check_pipeline.subprocess, "run") as run,
            ):
                if isinstance(failure, BaseException):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                self.assertEqual(check_pipeline.main(), 1)
                self.assertEqual(run.call_count, 1)
                self.assertIsInstance(run.call_args.args[0], list)
                self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_runner_checks_contracts_before_processes_and_completes_valid_checks(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(check_pipeline, "check_contracts", side_effect=ValueError),
            patch.object(check_pipeline.subprocess, "run") as run,
        ):
            self.assertEqual(check_pipeline.main(), 1)
            run.assert_not_called()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(
                check_pipeline.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
        ):
            self.assertEqual(check_pipeline.main(), 0)
            self.assertEqual(run.call_count, len(check_pipeline.steps()))
            forbidden = {"opencode", "gh", "ffmpeg", "ssh", "curl"}
            self.assertTrue(
                all(forbidden.isdisjoint(call.args[0]) for call in run.call_args_list)
            )
