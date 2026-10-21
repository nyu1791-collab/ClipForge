"""Boundary and failure tests; no remote services or inference calls."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from clipforge.batch import clipped_subtitles
from clipforge.batch_models import ClipsManifest
from clipforge.kick_media import child, parse_media
from clipforge.models import Transcript


class RichBatchTests(unittest.TestCase):
    def manifest(self):
        return json.loads(Path("jobs/examples/kick-test-3.json").read_text())

    def test_manifest_rejects_duplicates_outside_range_and_relative_subtitles(self):
        for change in ("duplicate", "outside", "relative", "overlap", "duration"):
            value = self.manifest()
            if change == "duplicate":
                value["clips"][1]["clip_id"] = "clip_01"
            elif change == "outside":
                value["source_duration"] = 100
            elif change == "relative":
                value["clips"][0]["subtitles"] = [
                    {"start": 1, "end": 2, "text": "wrong timebase"}
                ]
            elif change == "overlap":
                value["clips"][1].update(start_time=65, end_time=95)
            else:
                value["clips"][0]["end_time"] = 61
            with self.subTest(change=change), self.assertRaises(ValueError):
                ClipsManifest.model_validate(value)

    def test_segment_intersection_preserves_fractional_time(self):
        text = "#EXTM3U\n#EXTINF:10.669,\n0.ts\n#EXTINF:9.5,\n1.ts\n#EXT-X-ENDLIST\n"
        segments = parse_media(text, "https://stream.kick.com/a/list.m3u8", "/a/")
        selected = [s for s in segments if s["end"] > 10.7 and s["start"] < 15]
        self.assertEqual(len(selected), 1)
        self.assertAlmostEqual(10.7 - selected[0]["start"], 0.031)

    def test_unsafe_hls_features_and_paths_stop(self):
        for tag in (
            "#EXT-X-KEY",
            "#EXT-X-MAP",
            "#EXT-X-BYTERANGE",
            "#EXT-X-DISCONTINUITY",
        ):
            with self.subTest(tag=tag), self.assertRaises(RuntimeError):
                parse_media("#EXTM3U\n" + tag, "https://stream.kick.com/a/x", "/a/")
        for reference in (
            "https://evil.test/a.ts",
            "/other/x.ts",
            "a.ts?token=x",
            "../x.ts",
            "%2e%2e/x.ts",
        ):
            with self.subTest(reference=reference), self.assertRaises(RuntimeError):
                child("https://stream.kick.com/a/list.m3u8", reference, "/a/")

    def test_subtitle_clipping_does_not_shift_original_time(self):
        t = Transcript.model_validate(
            {
                "language": "ja",
                "segments": [
                    {
                        "start": 58,
                        "end": 62,
                        "text": "実際の発話",
                        "words": [{"start": 59, "end": 61, "text": "発話"}],
                    }
                ],
            }
        )
        result = clipped_subtitles(t, 60.3, 90.3)
        self.assertEqual(result[0].start, 60.3)
        self.assertEqual(result[0].words[0].start, 60.3)
        self.assertEqual(result[0].end, 62)
        self.assertEqual(result[0].text, "発話")

    def test_mount_sync_checksum_and_no_overwrite(self):
        spec = importlib.util.spec_from_file_location(
            "sync_assets", "tools/sync_assets.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mount = root / "mount"
            mount.mkdir()
            (mount / "sound.wav").write_bytes(b"original licensed asset")
            sha = hashlib.sha256((mount / "sound.wav").read_bytes()).hexdigest()
            item = {
                "path": "se/sound.wav",
                "source": "sound.wav",
                "sha256": sha,
                "license": "CC0-1.0",
            }
            result = module.sync(
                {"items": [item]}, root / "assets", root / "cache", [], mount
            )
            self.assertEqual(result[0]["status"], "imported")
            (mount / "sound.wav").write_bytes(b"changed")
            self.assertEqual(
                module.sync(
                    {"items": [item]}, root / "assets", root / "cache", [], mount
                )[0]["status"],
                "already_present",
            )
            with self.assertRaises(ValueError):
                module.sync(
                    {"items": [{**item, "path": "se/wrong.wav"}]},
                    root / "assets",
                    root / "cache",
                    [],
                    mount,
                )
            self.assertFalse((root / "assets/se/wrong.wav").exists())


if __name__ == "__main__":
    unittest.main()
