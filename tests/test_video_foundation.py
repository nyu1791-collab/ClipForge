"""Local regression checks. Real CPU rendering is confined to private workers."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pysubs2

from clipforge import captions
from clipforge.batch import run, speech_end
from clipforge.batch_models import ClipsManifest, ProductionPlan
from clipforge.batch_workflow import (
    automatic,
    cache_lock,
    cached_transcript,
    reviewed,
    transcribe_full,
)
from clipforge.caption_layout import NO_START, timed_units, units
from clipforge.media import frame_rate
from clipforge.models import SelectedClip, Settings, Transcript, Word
from clipforge.storage import PipelineError, digest, fingerprint, write_json
from clipforge.topic_selection import plan_topics

URL = "https://kick.com/example/videos/01a1574c-2350-77ce-90e2-4a7fefd4235c"


class VideoFoundationTests(unittest.TestCase):
    def reviewed_plan(self, metadata=False):
        short = json.loads(Path("jobs/examples/kick-test-3.json").read_text())
        topic = json.loads(Path("jobs/examples/kick-topics-test-3.json").read_text())
        short["selection_method"] = topic["selection_method"] = "manual"
        if not metadata:
            for manifest in (short, topic):
                for clip in manifest["clips"]:
                    clip["subtitles"] = [
                        {
                            "start": clip["start_time"],
                            "end": clip["start_time"] + 2,
                            "text": "合成の確認用発話",
                        }
                    ]
        return ProductionPlan(
            shorts=short, topics=topic, review_basis="assistant_transcript"
        )

    def transcript(self):
        topics = [
            "料理の材料を切り鍋に入れて煮込む夕食の準備",
            "宇宙の惑星と恒星の観測には望遠鏡が必要",
            "旅行のホテルと空港から電車に乗り観光地を歩く",
            "野球の投手が打者に向けて速球を投げホームラン",
            "自作パソコンのメモリと半導体を基板に接続",
            "音楽の楽譜を読みギターとピアノで歌を演奏",
            "小説の登場人物が推理して事件の犯人を探す",
            "川で魚を釣るため餌と針と竿を用意する",
            "農業の畑で野菜と果物を収穫し市場へ運ぶ",
            "病院の医師と看護師が患者の健康を診察する",
        ]
        return Transcript.model_validate(
            {
                "language": "ja",
                "segments": [
                    {
                        "start": t,
                        "end": t + 5,
                        "text": topics[t // 180] + "。",
                        "words": [
                            {"start": t, "end": t + 5, "text": topics[t // 180] + "。"}
                        ],
                    }
                    for t in range(0, 1800, 6)
                ],
            }
        )

    def test_topics_are_longer_nonoverlapping_and_end_at_speech_boundary(self):
        t = self.transcript()
        m = plan_topics(URL, 1800, t, count=3, seconds=180)
        self.assertEqual(m.output_kind, "topics")
        self.assertEqual(len(m.clips), 3)
        for c in m.clips:
            self.assertIn(c.end_time, {s.end for s in t.segments})
            self.assertTrue(150 <= c.end_time - c.start_time <= 210)
            self.assertTrue(
                all(c.start_time <= s.start < s.end <= c.end_time for s in c.subtitles)
            )
        for a, b in zip(m.clips, m.clips[1:]):
            self.assertLessEqual(a.end_time, b.start_time)

    def test_topic_plan_refuses_silence_or_insufficient_distinct_ranges(self):
        with self.assertRaises(PipelineError):
            plan_topics(URL, 1000, Transcript(language="ja", segments=[]))
        with self.assertRaises(PipelineError):
            plan_topics(URL, 40, self.transcript(), seconds=300)

    def test_profile_limits_and_schema_backwards_compatibility(self):
        value = json.loads(Path("jobs/examples/kick-test-3.json").read_text())
        self.assertEqual(ClipsManifest.model_validate(value).output_kind, "shorts")
        value["output_kind"] = "topics"
        with self.assertRaises(ValueError):
            ClipsManifest.model_validate(value)
        for i, c in enumerate(value["clips"]):
            c.update(type="topic", start_time=i * 400, end_time=i * 400 + 300)
        ClipsManifest.model_validate(value)
        value["output_kind"] = "shorts"
        with self.assertRaises(ValueError):
            ClipsManifest.model_validate(value)

    def test_source_lock_refuses_duplicate_analysis(self):
        with tempfile.TemporaryDirectory() as d, cache_lock(d, "example"):
            with self.assertRaises(PipelineError):
                with cache_lock(d, "example"):
                    pass

    def test_fps_preserves_25_and_fractional_rates_and_caps_high_rates(self):
        self.assertEqual(frame_rate("25/1"), "25")
        self.assertEqual(frame_rate("30000/1001"), "30000/1001")
        self.assertEqual(frame_rate("60000/1001"), "30")
        self.assertEqual(frame_rate("0/0"), "30")

    def test_morphology_rejoins_split_kana_and_preserves_whitespace_and_punctuation(
        self,
    ):
        for text in [
            "ちょうど話題が変わりました。",
            "AI と AGI を比較します。",
            "「料理の時間」です。",
            " hello  world ",
            "123万円です。",
        ]:
            self.assertEqual("".join(units(text)), text)
        words = [
            Word(start=0, end=0.3, text="ちょ"),
            Word(start=0.3, end=0.8, text="うど"),
            Word(start=0.8, end=1.5, text="話題が変わりました。"),
        ]
        parts, _ = timed_units(words)
        self.assertIn("ちょうど", [w.text for w in parts])
        self.assertTrue(all(w.text[0] not in NO_START for w in parts))

    def test_real_font_two_lines_srt_consistency_and_sanitization(self):
        t = Transcript.model_validate(
            {
                "language": "ja",
                "segments": [
                    {
                        "start": 100,
                        "end": 103.5,
                        "text": "ちょうど話題が変わりました。aiを紹介します。",
                        "words": [
                            {"start": 100, "end": 100.3, "text": " ちょ"},
                            {"start": 100.3, "end": 100.8, "text": "うど"},
                            {
                                "start": 100.8,
                                "end": 103.5,
                                "text": "話題が変わりました。aiを紹介します。",
                            },
                        ],
                    },
                    {"start": 105, "end": 106, "text": r"{\pos(0,0)}"},
                ],
            }
        )
        clip = SelectedClip(
            start=100,
            end=130,
            candidate_id="clip_01",
            title="料理の話題",
            reason="test",
            confidence=0,
        )
        for aspect in ("9:16", "16:9"):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                r = captions.create(
                    t,
                    clip,
                    root,
                    Settings(gemini_model="local", headline=True, aspect=aspect),
                )
                ass = pysubs2.load(str(root / "captions.ass"))
                srt = pysubs2.load(str(root / "captions.srt"))
                events = [e for e in ass if e.style == "Default"]
                self.assertEqual(
                    [(e.start, e.end, e.plaintext) for e in events],
                    [(e.start, e.end, e.plaintext) for e in srt],
                )
                self.assertTrue(all(len(e.plaintext.splitlines()) <= 2 for e in events))
                self.assertTrue(all(0 <= e.start < e.end <= 30000 for e in events))
                self.assertTrue(
                    all(a.end <= b.start for a, b in zip(events, events[1:]))
                )
                spoken = "".join(e.plaintext.replace("\n", "") for e in events)
                self.assertIn("ちょうど", spoken)
                self.assertIn("AI", spoken)
                self.assertNotIn(r"{\pos", spoken)
                self.assertEqual(r["missing_glyphs"], [])

    def test_no_speech_end_never_forces_a_mid_utterance_cut(self):
        t = Transcript.model_validate(
            {"language": "ja", "segments": [{"start": 0, "end": 200, "text": "long"}]}
        )
        self.assertEqual(speech_end(t, 0, 45, 300, 30, 180), 0)

    def test_unknown_long_sentence_uses_phrase_edges_without_losing_text(self):
        from unittest.mock import Mock

        import budoux

        text = "料理の材料について詳しく説明します。"
        tokenizer = Mock()
        tokenizer.tokenize.return_value = [text]
        with patch(
            "clipforge.caption_layout.engines",
            return_value=(tokenizer, budoux.load_default_japanese_parser()),
        ):
            parts = units(text)
            self.assertGreater(len(parts), 1)
            self.assertEqual("".join(parts), text)
            words, _ = timed_units([Word(start=0, end=6, text=text)])
            self.assertTrue(all(w.end - w.start <= 4 for w in words))

    def test_both_outputs_share_one_analysis_and_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            identity = fingerprint({"source": URL})
            audio = root / "cache" / "audio" / (identity + ".m4a")
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"cached-audio-fixture")
            write_json(
                audio.with_suffix(".json"),
                {"sha256": digest(audio), "source_duration": 1800},
            )

            def pick(url, duration, transcript, audio, count, seconds, kind):
                value = json.loads(Path("jobs/examples/kick-test-3.json").read_text())
                value["output_kind"] = kind
                if kind == "topics":
                    for i, c in enumerate(value["clips"]):
                        c.update(
                            type="topic", start_time=i * 400, end_time=i * 400 + 300
                        )
                return ClipsManifest.model_validate(value)

            with (
                patch(
                    "clipforge.batch_workflow.transcribe_full",
                    return_value=self.transcript(),
                ) as asr,
                patch("clipforge.batch_workflow.plan", side_effect=pick) as planner,
                patch(
                    "clipforge.batch_workflow.run",
                    return_value={"clips": [{}, {}, {}], "media_budget_used": 100},
                ) as renderer,
            ):
                result = automatic(
                    URL, root / "output", root / "cache", "assets", max_bytes=1000
                )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(asr.call_count, 1)
            self.assertEqual(planner.call_count, 2)
            self.assertEqual([c.args[-1] for c in renderer.call_args_list], [1000, 900])
            self.assertTrue((root / "output" / "production_result.json").is_file())

    def test_asr_checkpoints_reused_and_corrupt_chunk_recomputed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            audio = root / "audio.m4a"
            audio.write_bytes(b"fixture")
            identity = fingerprint(
                {
                    "audio": digest(audio),
                    "model": "small",
                    "language": "ja",
                    "chunk_seconds": 600,
                    "transcription_version": 1,
                }
            )
            from clipforge.storage import stage

            checkpoints = root / "transcripts" / identity
            for i, start in enumerate((0, 600)):
                length = 600 if i == 0 else 100
                key = fingerprint(
                    {"identity": identity, "start": start, "length": length}
                )
                stage(
                    checkpoints,
                    f"chunk_{i:04}",
                    key,
                    lambda: {
                        "language": "ja",
                        "segments": [
                            {
                                "start": start + 1,
                                "end": start + 2,
                                "text": "合成の確認用発話",
                            }
                        ],
                    },
                )
            with patch("faster_whisper.WhisperModel") as engine:
                self.assertEqual(
                    len(transcribe_full(audio, 700, root, "small").segments), 2
                )
                engine.assert_not_called()
            # Reviewed submissions must fail closed on incomplete/corrupt ASR,
            # rather than silently performing expensive inference again.
            saved_audio = root / "audio" / (fingerprint({"source": URL}) + ".m4a")
            saved_audio.parent.mkdir()
            saved_audio.write_bytes(audio.read_bytes())
            write_json(
                saved_audio.with_suffix(".json"),
                {"sha256": digest(audio), "source_url": URL, "source_duration": 700},
            )
            with patch("faster_whisper.WhisperModel") as engine:
                restored, _ = cached_transcript(root, URL, "small")
                self.assertEqual(len(restored.segments), 2)
                engine.assert_not_called()
            (checkpoints / "chunk_0001.json").write_text(
                '{"language":"ja","segments":[]}'
            )
            with self.assertRaises(PipelineError) as caught:
                cached_transcript(root, URL, "small")
            self.assertEqual(caught.exception.code, "asr_cache_integrity")
            (checkpoints / "chunk_0001.json").unlink()
            with self.assertRaises(PipelineError) as caught:
                cached_transcript(root, URL, "small")
            self.assertEqual(caught.exception.code, "asr_cache_required")
            with (
                patch("faster_whisper.WhisperModel") as engine,
                patch("clipforge.batch_workflow.process"),
                patch(
                    "clipforge.batch_workflow.transcribe_audio",
                    return_value=Transcript(language="ja", segments=[]),
                ) as asr,
            ):
                transcribe_full(audio, 700, root, "small")
                self.assertEqual(engine.call_count, 1)
                self.assertEqual(asr.call_count, 1)

    def test_cached_render_and_completed_resume_need_no_kick_connection(self):
        value = json.loads(Path("jobs/examples/kick-test-3.json").read_text())
        value["style"] = {}
        for c in value["clips"]:
            c["subtitles"] = [
                {
                    "start": c["start_time"] + 1,
                    "end": c["start_time"] + 2,
                    "text": "合成の確認用",
                }
            ]
        manifest = ClipsManifest.model_validate(value)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / "cache"
            for c in manifest.clips:
                key = fingerprint(
                    {
                        "url": manifest.source_url,
                        "start": c.start_time,
                        "end": c.end_time,
                        "quality": "720p",
                    }
                )
                source = cache / "ranges" / (key + ".mp4")
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"synthetic-source-fixture")
                write_json(
                    source.with_suffix(".json"),
                    {
                        "sha256": digest(source),
                        "source_duration": manifest.source_duration,
                    },
                )

            def render_fixture(source, staging, clip, *args):
                staging.mkdir()
                for name in (
                    "video.mp4",
                    "captions.ass",
                    "captions.srt",
                    "caption_review.json",
                ):
                    (staging / name).write_bytes(b"synthetic-render-fixture")
                return {"clip": clip.model_dump()}

            with (
                patch("clipforge.batch.KickVOD") as vod,
                patch("clipforge.batch.render", side_effect=render_fixture) as render,
            ):
                run(manifest, root / "output", cache, "assets")
                run(manifest, root / "output", cache, "assets", resume=True)
                vod.assert_not_called()
                self.assertEqual(render.call_count, 3)
                (root / "output" / "clip_01" / "video.mp4").write_bytes(b"tampered")
                with self.assertRaises(PipelineError):
                    run(manifest, root / "output", cache, "assets", resume=True)

    def test_true_peak_gate_rejects_output_even_if_decoder_exits_successfully(self):
        from types import SimpleNamespace

        from clipforge.quality import inspect_video

        probe = {
            "width": 404,
            "height": 720,
            "duration": 30,
            "audio": True,
            "audio_sample_rate": 48000,
        }
        report = SimpleNamespace(
            stderr=json.dumps({"input_i": "-16.0", "input_tp": "-0.34"}).encode()
        )
        with (
            patch("clipforge.quality.media.probe", return_value=probe),
            patch("clipforge.quality.process", return_value=report),
        ):
            with self.assertRaises(PipelineError):
                inspect_video("synthetic.mp4", 30, 404, 720)

    def test_audio_gate_rejects_implicit_96khz_encoder_output(self):
        from clipforge.quality import inspect_video

        info = {
            "width": 404,
            "height": 720,
            "duration": 30,
            "audio": True,
            "audio_sample_rate": 96000,
        }
        with (
            patch("clipforge.quality.media.probe", return_value=info),
            patch("clipforge.quality.process") as decode,
        ):
            with self.assertRaises(PipelineError) as caught:
                inspect_video("synthetic.mp4", 30, 404, 720)
            self.assertEqual(caught.exception.code, "audio_sample_rate")
            decode.assert_not_called()

    def test_reviewed_bundle_rejects_different_sources_and_auto_provenance(self):
        p = self.reviewed_plan().model_dump()
        p["topics"]["source_url"] = URL
        with self.assertRaises(ValueError):
            ProductionPlan.model_validate(p)
        p = self.reviewed_plan().model_dump()
        p["shorts"]["selection_method"] = "local_transcript_audio"
        with self.assertRaises(ValueError):
            ProductionPlan.model_validate(p)

    def test_reviewed_bundle_shares_budget_without_global_asr(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch("clipforge.batch_workflow.transcribe_full") as asr,
            patch(
                "clipforge.batch_workflow.run",
                return_value={"clips": [{}, {}, {}], "media_budget_used": 100},
            ) as render,
        ):
            result = reviewed(
                self.reviewed_plan(),
                Path(d) / "out",
                Path(d) / "cache",
                "assets",
                max_bytes=1000,
            )
            asr.assert_not_called()
            self.assertEqual([x.args[-1] for x in render.call_args_list], [1000, 900])
            self.assertEqual(result["review_basis"], "assistant_transcript")
            self.assertFalse(result["human_review_performed"])

    def test_private_plan_submission_is_idempotent_and_refuses_public_repo(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "submit_rich_batch", "tools/submit_rich_batch.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.json"
            write_json(path, self.reviewed_plan(metadata=True).model_dump())

            def invoke(*args):
                return json.dumps({"private": True}) if args[0] == "api" else ""

            with patch.object(module, "gh", side_effect=invoke) as gh:
                a = module.submit(path, "a" * 40, "123", state=Path(d) / "state")
                path.write_text(
                    json.dumps(
                        self.reviewed_plan(metadata=True).model_dump(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                b = module.submit(path, "a" * 40, "123", state=Path(d) / "state")
                self.assertEqual(a, b)
                self.assertEqual(
                    sum(c.args[0] == "workflow" for c in gh.call_args_list), 1
                )
                self.assertEqual(
                    sum(c.args[0] == "release" for c in gh.call_args_list), 0
                )
                dispatch = next(c for c in gh.call_args_list if c.args[0] == "workflow")
                wire = json.loads(
                    next(
                        s.split("=", 1)[1]
                        for s in dispatch.args
                        if isinstance(s, str) and s.startswith("plan_json=")
                    )
                )
                self.assertTrue(
                    all(
                        not c["subtitles"]
                        for k in ("shorts", "topics")
                        for c in wire[k]["clips"]
                    )
                )
            with patch.object(
                module, "gh", return_value=json.dumps({"private": False})
            ) as gh:
                with self.assertRaises(PipelineError):
                    module.submit(path, "b" * 40, "123", state=Path(d) / "public")
                self.assertEqual(gh.call_count, 1)
            write_json(path, self.reviewed_plan().model_dump())
            with patch.object(module, "gh") as gh:
                with self.assertRaises(PipelineError) as caught:
                    module.submit(path, "a" * 40, "123", state=Path(d) / "speech")
                self.assertEqual(caught.exception.code, "interval_metadata_required")
                gh.assert_not_called()

    def test_reviewed_intervals_restore_speech_once_without_asr(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch(
                "clipforge.batch_workflow.cached_transcript",
                return_value=(self.transcript(), {"source_duration": 7864.686}),
            ) as cache,
            patch("clipforge.batch_workflow.transcribe_full") as asr,
            patch(
                "clipforge.batch_workflow.run",
                return_value={"clips": [{}, {}, {}], "media_budget_used": 100},
            ) as render,
        ):
            result = reviewed(
                self.reviewed_plan(metadata=True),
                Path(d) / "out",
                Path(d) / "cache",
                "assets",
            )
            cache.assert_called_once()
            asr.assert_not_called()
            self.assertTrue(result["asr_cache_reused"])
            self.assertFalse(result["global_asr_performed"])
            self.assertTrue(
                all(
                    c.subtitles
                    for call in render.call_args_list
                    for c in call.args[0].clips
                )
            )


if __name__ == "__main__":
    unittest.main()
