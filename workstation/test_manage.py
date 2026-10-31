import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import manage


class PortableWorkstationTests(unittest.TestCase):
    def test_package_keeps_originals_subtitle_times_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(b"synthetic-media")
            subtitle = "1\n00:00:01,250 --> 00:00:03,000\n字幕の内容\n"
            (source / "captions.srt").write_text(subtitle, encoding="utf-8")
            (source / ".env").write_text("GEMINI_API_KEY=do-not-copy", encoding="utf-8")
            result = manage.package(source, root / "kit", "tiktok", "見出し")
            self.assertEqual(result["clips"], 1)
            self.assertEqual(
                (root / "kit/clips/clip-001/captions.srt").read_text(encoding="utf-8"),
                subtitle,
            )
            self.assertTrue((source / "video.mp4").is_file())
            self.assertFalse(any(p.name == ".env" for p in (root / "kit").rglob("*")))
            manifest = json.loads((root / "kit/manifest.json").read_text())
            self.assertEqual(manifest["clips"][0]["directory"], "clips/clip-001")
            self.assertEqual(manifest["clips"][0]["title"], "見出し")
            self.assertFalse(
                json.loads((root / "kit/package.json").read_text())["automatic_posting"]
            )

    def test_package_does_not_mislabel_mov_or_overwrite_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "movie.mov").write_bytes(b"mov")
            manage.package(source, root / "kit")
            self.assertTrue((root / "kit/clips/clip-001/video.mov").exists())
            self.assertEqual(
                json.loads((root / "kit/manifest.json").read_text())["clips"], []
            )
            with self.assertRaises(ValueError):
                manage.package(source, root / "kit")
            with self.assertRaises(ValueError):
                manage.package(source, source / "nested")

    def test_windows_relink_preserves_timing_ids_text_and_other_roots(self):
        value = {
            "materials": [
                {"path": "C:\\Old\\media\\video.mp4", "id": "v1", "start": 12345}
            ],
            "text": "C:\\Old は説明文",
            "file_path": "C:/Older/a.mp4",
            "url": "https://example.test/C:/Old",
        }
        changed = manage.relink(value, "c:\\old", "D:\\移行先")
        self.assertEqual(changed["materials"][0]["path"], "D:/移行先/media/video.mp4")
        self.assertEqual(changed["materials"][0]["start"], 12345)
        self.assertEqual(changed["materials"][0]["id"], "v1")
        self.assertEqual(changed["text"], value["text"])
        self.assertEqual(changed["file_path"], value["file_path"])
        self.assertEqual(changed["url"], value["url"])
        with self.assertRaises(ValueError):
            manage.replace_path("C:/Old/../secret", "C:/Old", "D:/new")

    def test_posix_to_windows_relink(self):
        self.assertEqual(
            manage.replace_path(
                "/workspace/jobs/media/a.mp4", "/workspace/jobs", "C:\\work"
            ),
            "C:/work/media/a.mp4",
        )
        self.assertEqual(
            manage.relink({"file_Path": "C:/Old/media.mp4"}, "C:/Old", "D:/New"),
            {"file_Path": "D:/New/media.mp4"},
        )

    def test_postiz_payload_is_draft_and_requires_uploaded_media(self):
        result = manage.postiz_draft(
            "channel", "見出し", "uploaded-id", "https://media.example.test/a.mp4"
        )
        self.assertEqual(result["type"], "draft")
        self.assertEqual(result["posts"][0]["integration"]["id"], "channel")
        self.assertEqual(
            result["posts"][0]["value"][0]["image"][0]["id"], "uploaded-id"
        )
        with self.assertRaises(ValueError):
            manage.postiz_draft("channel", "text", "id", "C:/private/a.mp4")

    def test_setup_defaults_to_no_execution(self):
        with patch.object(manage.subprocess, "run") as execute:
            result = manage.run_setup()
        execute.assert_not_called()
        self.assertFalse(result["executed"])
        self.assertTrue(result["commands"])

    def test_doctor_never_returns_credential_values(self):
        names = manage.profile().get("credential_names", [])
        name = names[0] if names else "GEMINI_API_KEY"
        with patch.dict("os.environ", {name: "secret-that-must-not-appear"}):
            result = manage.doctor()
        if names:
            self.assertTrue(result["credential_variable_present"][name])
        serialized = json.dumps(result)
        self.assertNotIn("secret-that-must-not-appear", serialized)

    def test_crawl_adapter_saves_provenance_and_failed_pages(self):
        path = Path(__file__).with_name("research.py")
        if not path.exists():
            self.skipTest("Optional research adapter not selected for this repository")
        spec = importlib.util.spec_from_file_location("optional_research", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Crawler:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def arun(self, url, config):
                return SimpleNamespace(
                    success="good" in url,
                    status_code=200 if "good" in url else 404,
                    markdown=SimpleNamespace(raw_markdown="# 根拠となる資料"),
                )

        fake = SimpleNamespace(
            AsyncWebCrawler=Crawler,
            BrowserConfig=lambda **k: k,
            CacheMode=SimpleNamespace(BYPASS="bypass"),
            CrawlerRunConfig=lambda **k: k,
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(sys.modules, {"crawl4ai": fake}),
        ):
            out = Path(temporary) / "research"
            result = asyncio.run(
                module.collect(
                    ["https://good.example.test", "https://bad.example.test"], out
                )
            )
            self.assertEqual(result["llm_requests"], 0)
            self.assertEqual(result["successful"], 1)
            self.assertEqual(
                (out / "source-001.md").read_text(encoding="utf-8"), "# 根拠となる資料"
            )
            sources = json.loads((out / "sources.json").read_text())
            self.assertEqual(sources["sources"][1]["http_status"], 404)
        with self.assertRaises(ValueError):
            module.source_url("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
