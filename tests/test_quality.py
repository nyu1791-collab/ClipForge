"""Regression cases for source geometry, Japanese layout and hidden future subtitles."""

from pathlib import Path
import tempfile
import unittest

import pysubs2
from pydantic import ValidationError

from clipforge.captions import create
from clipforge.layout import filter_graph, output_size, source_rect
from clipforge.models import Settings, SelectedClip, SourceRegion, Transcript
from clipforge.storage import PipelineError
from clipforge.typography import (
    resolve_font,
    safe,
    valid_break,
    with_breaks,
    wrap_offsets,
)


def settings(**changes):
    return Settings(gemini_model="manual-no-api", **changes)


def clip(start=10, end=16):
    return SelectedClip(
        candidate_id="manual",
        title="確認",
        reason="fixture",
        confidence=1,
        start=start,
        end=end,
    )


def transcript():
    return Transcript.model_validate(
        {
            "language": "ja",
            "segments": [
                {
                    "start": 10,
                    "end": 15,
                    "text": "親になった途端に教育を語り出す。",
                    "words": [
                        {"start": 10, "end": 10.7, "text": "親に"},
                        {"start": 10.7, "end": 11.5, "text": "なった"},
                        {"start": 11.5, "end": 12.0, "text": "途端に"},
                        {"start": 13, "end": 13.7, "text": "教育を"},
                        {"start": 13.7, "end": 15, "text": "語り出す。"},
                    ],
                }
            ],
        }
    )


class GeometryTests(unittest.TestCase):
    def test_full_hd_vertical_and_legacy_even_dimensions(self):
        self.assertEqual(output_size(settings()), (1080, 1920))
        self.assertEqual(output_size(settings(height=720)), (404, 720))

    def test_regions_reject_outside_nan_and_zero(self):
        for value in [
            dict(x=0.9, y=0, width=0.2, height=0.2),
            dict(x=0, y=0, width=0, height=0.2),
            dict(x=float("nan"), y=0, width=0.2, height=0.2),
        ]:
            with self.assertRaises(ValidationError):
                SourceRegion(**value)

    def test_subpixel_region_is_not_silently_expanded(self):
        region = SourceRegion(x=0.5, y=0.5, width=0.001, height=0.001)
        with self.assertRaises(PipelineError):
            source_rect(region, 640, 360)

    def test_crop_stays_inside_odd_source(self):
        for x in [0, 0.1, 0.7]:
            region = SourceRegion(x=x, y=0.3, width=1 - x, height=0.7)
            for zoom in [1, 1.6]:
                rect = source_rect(region, 853, 479, zoom)
                self.assertLessEqual(rect.x + rect.width, 853)
                self.assertLessEqual(rect.y + rect.height, 479)
                self.assertTrue(all(v % 2 == 0 for v in vars(rect).values()))

    def test_punch_in_uses_original_time_and_excludes_non_intersections(self):
        s = settings(
            framing="stacked",
            presenter_region=dict(x=0, y=0.6, width=0.3, height=0.4),
            punch_ins=[dict(start=11, end=13, zoom=1.3), dict(start=20, end=22)],
        )
        graph, receipt = filter_graph(
            dict(width=1280, height=720, video_index=0), clip(), s
        )
        self.assertEqual(receipt["punch_ins"], [dict(start=1, end=3, zoom=1.3)])
        self.assertIn("gte(t,1.000000)*lt(t,3.000000)", graph)
        self.assertEqual(len(receipt["regions"]), 3)
        zoom = receipt["regions"][-1]["source"]
        base = receipt["regions"][1]["source"]
        self.assertLess(zoom["width"], base["width"])

    def test_ambiguous_layouts_are_rejected(self):
        for changes in [
            dict(framing="stacked"),
            dict(
                framing="stacked",
                aspect="16:9",
                presenter_region=dict(x=0, y=0, width=0.2, height=0.2),
            ),
            dict(punch_ins=[dict(start=1, end=2)]),
            dict(
                framing="stacked",
                presenter_region=dict(x=0, y=0, width=0.2, height=0.2),
                punch_ins=[dict(start=1, end=3), dict(start=2, end=4)],
            ),
        ]:
            with self.assertRaises(ValidationError):
                settings(**changes)

    def test_background_and_audio_branches_are_not_duplicated(self):
        graph, _ = filter_graph(
            dict(width=1280, height=720, video_index=2), clip(), settings()
        )
        self.assertIn("[0:2]", graph)
        self.assertIn("gblur", graph)
        self.assertEqual(graph.count("[video]"), 1)
        self.assertNotIn("0:a", graph)


class TypographyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.face = resolve_font("Noto Sans CJK JP")
        cls.font = cls.face.at(48)

    def test_missing_font_does_not_silently_fall_back(self):
        with self.assertRaises(PipelineError):
            resolve_font("ClipForge Missing Font 999999")

    def test_measured_mixed_japanese_wrap_preserves_text_and_width(self):
        for text in [
            "親になった途端に教育を語り出すんだよね。",
            "「Apex Legendsの大会」について話そう！",
            "ＷＷＷとWWW、全角と半角で幅が違います。",
            "Discordで設定を変えたら「画面共有」ができた。",
        ]:
            offsets, _ = wrap_offsets(text, self.font, 450)
            lines = with_breaks(text, offsets, "\n").splitlines()
            self.assertEqual("".join(lines), text)
            self.assertTrue(all(self.font.getlength(line) <= 450.01 for line in lines))
            for end in offsets[:-1]:
                self.assertTrue(valid_break(text, end))

    def test_combining_mark_is_not_left_at_start_of_line(self):
        text = "か\u3099くせいの話を聞いてみよう。"
        offsets, _ = wrap_offsets(text, self.font, 220)
        self.assertNotIn(1, offsets)

    def test_override_and_control_text_is_inert(self):
        text = safe("{\\pos(0,0)}教育\nテスト\u0001")
        self.assertNotIn("{", text)
        self.assertNotIn("\\", text)
        self.assertNotIn("\n", text)
        self.assertNotIn("\u0001", text)

    def test_font_reports_unavailable_glyphs(self):
        self.assertEqual(self.face.missing("日本語の字幕ABC"), [])
        self.assertIn("U+10FFFF", self.face.missing("\U0010ffff"))


class CaptionTests(unittest.TestCase):
    def render(self, source=None, **changes):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        receipt = create(source or transcript(), clip(), path, settings(**changes))
        return (
            pysubs2.load(str(path / "captions.ass")),
            pysubs2.load(str(path / "captions.srt"), format_="srt"),
            receipt,
        )

    def test_progressive_srt_does_not_reveal_the_payoff_early(self):
        ass, srt, receipt = self.render()
        early = [e for e in srt if e.start < 3000]
        self.assertTrue(early)
        self.assertTrue(all("教育" not in e.plaintext for e in early))
        self.assertTrue(any("教育" in e.plaintext for e in srt if e.start >= 3000))
        self.assertTrue(any(r"\alpha&HFF&\bord0" in e.text for e in ass))
        self.assertEqual(receipt["mode"], "progressive")

    def test_captions_are_bounded_and_no_more_than_two_lines(self):
        for mode in ["plain", "karaoke", "progressive"]:
            ass, srt, receipt = self.render(caption=mode)
            self.assertTrue(srt)
            for event in srt:
                self.assertGreater(event.end, event.start)
                self.assertGreaterEqual(event.start, 0)
                self.assertLessEqual(event.end, 6000)
                self.assertLessEqual(len(event.plaintext.splitlines()), 2)
            for a, b in zip(srt, srt[1:]):
                self.assertLessEqual(a.end, b.start)
            self.assertFalse(receipt["missing_glyphs"])

    def test_untimed_long_asr_is_marked_for_review(self):
        source = Transcript.model_validate(
            {
                "language": "ja",
                "segments": [
                    {
                        "start": 10,
                        "end": 16,
                        "text": "親になった途端に教育を語り出すという話をしています。",
                    }
                ],
            }
        )
        ass, srt, receipt = self.render(source)
        self.assertIn("estimated_caption_timing", receipt["review_required"])
        self.assertTrue(receipt["estimated_timing_spans"])
        self.assertTrue(srt)

    def test_no_asr_timing_is_invented_for_an_unsplit_word(self):
        source = Transcript.model_validate(
            {
                "language": "en",
                "segments": [
                    {
                        "start": 10.25,
                        "end": 11,
                        "text": "Hello",
                        "words": [dict(start=10.25, end=11, text="Hello")],
                    }
                ],
            }
        )
        _, srt, receipt = self.render(source)
        self.assertEqual((srt[0].start, srt[0].end), (250, 1000))
        self.assertNotIn("estimated_caption_timing", receipt["review_required"])

    def test_headline_is_separate_and_never_exported_as_spoken_srt(self):
        ass, srt, receipt = self.render(headline="親になって変わった話")
        title = [e for e in ass if e.style == "Headline"]
        self.assertEqual(len(title), 1)
        self.assertEqual((title[0].start, title[0].end), (0, 6000))
        self.assertTrue(all("変わった話" not in e.text for e in srt))
        self.assertTrue(receipt["headline_rendered"])

    def test_empty_silent_manual_clip_needs_no_font(self):
        source = Transcript(language="und", segments=[])
        ass, srt, receipt = self.render(source, font="Intentionally Missing")
        self.assertFalse(ass.events)
        self.assertFalse(srt.events)
        self.assertIsNone(receipt["font"])

    def test_excessive_title_and_font_size_fail_before_encoding(self):
        with self.assertRaises(PipelineError):
            self.render(headline="長い見出し" * 24)
        with self.assertRaises(PipelineError):
            self.render(
                framing="stacked",
                caption_font_scale=0.055,
                presenter_region=dict(x=0, y=0, width=0.2, height=0.2),
            )


if __name__ == "__main__":
    unittest.main()
