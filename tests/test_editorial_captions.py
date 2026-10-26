import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pysubs2
import pytest
from PIL import Image

from clipforge import audio_first, captions, gemini_audio_editor, pipeline
from clipforge import hardware, worker_audio
from clipforge.editorial_text import minimal_edits
from clipforge.models import Segment, SelectedClip, Settings, Transcript, Word
from clipforge.speaker_captions import (
    assignments_to_turns,
    speaker_for_token,
    validate_turns,
)
from clipforge.storage import PipelineError
from clipforge.terminology import grounded_terms


def selection(start=10, end=30):
    return SelectedClip(
        start=start, end=end, candidate_id="one", title="対話", reason="r", confidence=1
    )


def test_removing_fillers_keeps_the_following_word_clock_and_original():
    words = [
        Word(start=10, end=10.5, text="えー"),
        Word(start=10.5, end=12, text="南くんが"),
        Word(start=12, end=12.4, text="あのー"),
        Word(start=12.4, end=14, text="来ます。"),
    ]
    segment = Segment(
        start=10, end=14, text="".join(w.text for w in words), words=words
    )
    transcript = Transcript(language="ja", segments=[segment])
    original = transcript.model_dump()
    edits = minimal_edits(segment, "南くんが来ます。", 0)
    changed = audio_first.edited_transcript(transcript, 10, edits)
    assert changed.segments[0].text == "南くんが来ます。"
    assert changed.segments[0].words == [words[1], words[3]]
    assert transcript.model_dump() == original


def test_name_correction_preserves_surrounding_words_and_whole_filler_can_disappear():
    words = [
        Word(start=10, end=11, text="それは"),
        Word(start=11, end=12, text="南"),
        Word(start=12, end=14, text="くんです。"),
    ]
    segment = Segment(start=10, end=14, text="それは南くんです。", words=words)
    transcript = Transcript(language="ja", segments=[segment])
    changed = audio_first.edited_transcript(
        transcript, 10, minimal_edits(segment, "それはミナミくんです。", 0)
    )
    assert changed.segments[0].text == "それはミナミくんです。"
    assert changed.segments[0].words[0] == words[0]
    assert changed.segments[0].words[-1] == words[-1]
    assert changed.segments[0].words[1].start == 11
    assert changed.segments[0].words[-2].end == 12
    assert not audio_first.edited_transcript(
        transcript, 10, minimal_edits(segment, "", 0)
    ).segments


@pytest.mark.parametrize(
    "before,after",
    [
        ("今日は行く", "今日は行くよ"),
        ("来る", "すぐ来る"),
        ("えー来るあの行く", "来る行く"),
    ],
)
def test_word_aligned_edits_reconstruct_cleaned_text(before, after):
    segment = Segment(
        start=0,
        end=len(before),
        text=before,
        words=[Word(start=i, end=i + 1, text=c) for i, c in enumerate(before)],
    )
    transcript = Transcript(language="ja", segments=[segment])
    changed = audio_first.edited_transcript(
        transcript, 0, minimal_edits(segment, after, 0)
    )
    assert "".join(s.text for s in changed.segments) == after


def test_only_cited_grounded_names_are_offered_to_the_audio_editor():
    url = "https://kick.com/example"
    row = {
        "surface": "南くん",
        "preferred": "ミナミくん",
        "source_urls": [url],
        "evidence": "本人プロフィールと配信の文脈",
        "confidence": 0.95,
    }
    grounding = {
        "groundingChunks": [{"web": {"uri": url}}],
        "groundingSupports": [{"groundingChunkIndices": [0]}],
    }
    terms, report = grounded_terms({"terms": [row]}, grounding, "南くんが来る")
    assert terms[0]["preferred"] == "ミナミくん" and report["accepted"] == 1
    for missing in ({}, {"groundingChunks": grounding["groundingChunks"]}):
        assert grounded_terms({"terms": [row]}, missing, "南くんが来る")[0] == []
    row["source_urls"] = ["https://invented.example/name"]
    assert grounded_terms({"terms": [row]}, grounding, "南くんが来る")[0] == []
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/example"
    grounded_redirect = {
        "groundingChunks": [{"web": {"uri": redirect}}],
        "groundingSupports": [
            {
                "groundingChunkIndices": [0],
                "segment": {"text": "南くんの活動名はミナミくん"},
            }
        ],
    }
    # A term-specific provider citation can replace the model's destination link.
    terms, _ = grounded_terms({"terms": [row]}, grounded_redirect, "南くんが来る")
    assert terms[0]["source_urls"] == [redirect]


def test_speaker_ids_are_stable_across_clips_and_bad_assignments_are_neutral():
    slots = {
        "a": {"clip": 1, "start": 0, "end": 1},
        "b": {"clip": 1, "start": 1, "end": 2},
        "c": {"clip": 2, "start": 0, "end": 1},
        "d": {"clip": 2, "start": 1, "end": 2},
    }
    rows = [
        {"segment_id": sid, "speaker_id": "speaker_2", "confidence": 0.95}
        for sid in slots
    ]
    turns, report = assignments_to_turns(
        {"audio_access": "heard", "speaker_assignments": rows}, slots, 2, {"b"}
    )
    assert turns[0][0]["speaker_id"] == turns[1][0]["speaker_id"] == "speaker_2"
    assert speaker_for_token(1, 2, turns[0]) is None
    assert speaker_for_token(0, 2, turns[0]) is None
    assert report["accepted"] == 3
    turns, report = assignments_to_turns(
        {
            "audio_access": "heard",
            "speaker_assignments": rows + [rows[0], {"segment_id": "invented"}],
        },
        slots,
        2,
    )
    assert turns[0][0]["speaker_id"] is None
    assert report["duplicate_ids"] == ["a"] and report["unknown_ids"] == ["invented"]


@pytest.mark.parametrize("value", [float("nan"), True, -1, 20])
def test_invalid_speaker_timing_stops_before_render(value):
    with pytest.raises(PipelineError):
        validate_turns(
            [{"start": value, "end": 10, "speaker_id": "speaker_1", "confidence": 0.9}],
            10,
        )


@pytest.mark.parametrize("mode", ["plain", "karaoke", "sequential"])
def test_wide_captions_keep_speaker_changes_separate_and_srt_plain(tmp_path, mode):
    settings = Settings(aspect="16:9", height=360, caption=mode, caption_scale=1.6)
    texts = [
        "今日はみんなで次の企画について少し詳しく話してみましょう。",
        "その話なら私からも伝えたいことがあります。",
        "うん、そうだね。",
    ]
    segments = [
        Segment(
            start=10 + i * 5,
            end=15 + i * 5,
            text=t,
            words=[Word(start=10 + i * 5, end=15 + i * 5, text=t)],
        )
        for i, t in enumerate(texts)
    ]
    turns = [
        {"start": i * 5, "end": (i + 1) * 5, "speaker_id": sp, "confidence": 0.95}
        for i, sp in enumerate(["speaker_2", "speaker_3", None])
    ]
    result = captions.create(
        Transcript(language="ja", segments=segments),
        selection(),
        tmp_path,
        settings,
        speaker_turns=turns,
    )
    ass = pysubs2.load(str(tmp_path / "captions.ass"))
    assert [e.style for e in ass] == [
        "speaker_2",
        "speaker_3",
        "Sequential" if mode == "sequential" else "Default",
    ]
    assert ass[0].end <= ass[1].start == 5000
    assert result["speaker_colored_events"] == 2
    assert any(len(line) > 13 for line in ass[0].plaintext.splitlines())
    assert all(len(e.plaintext.splitlines()) <= 3 for e in ass)
    assert ass.styles["speaker_2"].primarycolor == pysubs2.Color(110, 220, 255)
    assert ass.styles["speaker_2"].secondarycolor == pysubs2.Color(
        110, 220, 255, 255 if mode == "sequential" else 0
    )
    assert all("{" not in e.text for e in pysubs2.load(str(tmp_path / "captions.srt")))


def test_short_sentences_join_and_third_line_completes_the_phrase(monkeypatch):
    class Measure:
        def width(self, text):
            return len(text)

    monkeypatch.setattr(captions, "sentence_ends", lambda tokens: {0, 2})
    tokens = [(0, 0.7, "今日は"), (0.7, 1.4, "この話を"), (1.4, 3, "続けます")]
    groups = captions.group(
        tokens, Measure(), 5, max_lines=3, min_seconds=2.5, max_seconds=8
    )
    assert groups == [tokens]
    assert (
        len(captions.wrap_breaks("".join(t[2] for t in groups[0]), Measure(), 5)) + 1
        == 3
    )


def test_ass_render_uses_most_of_the_width_without_cutting_off_glyphs(tmp_path):
    text = "この企画については最初の説明から参加する人たちの話まで途中で細かく切り替えずに読めるようにまとめて表示します。"
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(start=0, end=7, text=text, words=[Word(start=0, end=7, text=text)])
        ],
    )
    captions.create(
        transcript,
        selection(0, 10),
        tmp_path,
        Settings(aspect="16:9", height=360, caption="plain", caption_scale=1.6),
    )
    target = tmp_path / "preview.png"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x360:r=1:d=1",
            "-vf",
            f"ass={tmp_path}/captions.ass",
            "-frames:v",
            "1",
            "-threads",
            "1",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    with Image.open(target) as image:
        bounds = (
            image.convert("L").point(lambda pixel: 255 if pixel > 60 else 0).getbbox()
        )
    assert bounds is not None
    left, _, right, _ = bounds
    assert left >= 10 and right <= 630
    assert right - left > 480


def test_saved_review_metadata_passes_selection_validation():
    value = {
        "analysis_key": "key",
        "clips": [
            {
                **selection(0, 300).model_dump(),
                "speaker_turns": [
                    {
                        "start": 0,
                        "end": 10,
                        "speaker_id": "speaker_2",
                        "confidence": 0.9,
                    }
                ],
            }
        ],
    }
    value["clips"] = [
        {
            **value["clips"][0],
            "candidate_id": f"one-{i}",
            "start": i * 300,
            "end": (i + 1) * 300,
        }
        for i in range(5)
    ]
    result = audio_first.reviewed_clips(
        value,
        {
            "analysis_key": "key",
            "duration": 1500,
            "topics": [
                {"id": f"one-{i}", "start": i * 300, "end": (i + 1) * 300}
                for i in range(5)
            ],
        },
        {"analysis_key": "key", "mode": "transcript", "previews": []},
        Settings(),
    )
    assert result[0].candidate_id == "one-0"


def test_render_worker_restores_audio_editor_topics_without_losing_speakers(
    tmp_path, monkeypatch
):
    source_url = "https://kick.com/example/videos/saved"
    value = {
        "analysis_key": "key",
        "source_url": source_url,
        "selection_basis": "saved_asr_audio_review",
        "clips": [
            {
                **selection(i * 300, (i + 1) * 300).model_dump(),
                "candidate_id": f"saved-{i}",
                "caption_edits": [],
                "speaker_turns": [
                    {"start": 0, "end": 2, "speaker_id": "speaker_2", "confidence": 0.9}
                ],
            }
            for i in range(5)
        ],
    }
    review = tmp_path / "selection.json"
    review.write_text(json.dumps(value))

    def restore(previous, output, previous_id):
        (output / "audio.m4a").write_bytes(b"saved")
        (output / "asr-receipt.json").write_text(
            json.dumps({"key": "saved", "reused": True})
        )
        return source_url

    analysis = {"analysis_key": "key", "duration": 1500, "topics": []}
    monkeypatch.setattr(worker_audio, "restore_analysis", restore)
    monkeypatch.setattr(hardware, "video_options", lambda *args: [])
    monkeypatch.setattr(
        audio_first,
        "allocate_audio",
        lambda *args: (analysis, Transcript(language="ja", segments=[])),
    )
    monkeypatch.setattr(
        worker_audio,
        "open_source",
        lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(audio_first, "discard_transport_cache", lambda *args: None)

    class ReachedRenderer(Exception):
        pass

    def render(vod, output, settings, review_file, **kwargs):
        preview = json.loads((output / "previews.json").read_text())
        selected = audio_first.reviewed_clips(value, analysis, preview, settings)
        assert len(selected) == 5
        assert (
            json.loads(review_file.read_text())["clips"][0]["speaker_turns"][0][
                "speaker_id"
            ]
            == "speaker_2"
        )
        raise ReachedRenderer

    monkeypatch.setattr(audio_first, "render", render)
    with pytest.raises(ReachedRenderer):
        worker_audio.execute(
            tmp_path / "previous",
            tmp_path / "out",
            "old",
            Settings(aspect="16:9", height=360),
            "render",
            review_file=review,
        )


def test_speaker_metadata_changes_render_cache_key_before_reusing_a_clip(
    tmp_path, monkeypatch
):
    # A receipt for another voice must not be treated as the same burned subtitle video.
    seen = []
    monkeypatch.setattr(
        pipeline,
        "_existing_clip",
        lambda target, name, saved, key: seen.append(key) or {},
    )
    monkeypatch.setattr(
        pipeline, "_stored_inputs", lambda saved: {"chat": None, "layout": None}
    )
    (tmp_path / "clip-001").mkdir()
    (tmp_path / "clip-001" / "render.json").write_text("{}")
    transcript = Transcript(language="ja", segments=[])
    for speaker in ["speaker_1", "speaker_2"]:
        pipeline.render_clip(
            Path("unused"),
            tmp_path,
            1,
            selection(),
            transcript,
            Settings(),
            "job",
            speaker_turns=[
                {"start": 0, "end": 2, "speaker_id": speaker, "confidence": 0.9}
            ],
        )
    assert len(set(seen)) == 2


def test_audio_editor_researches_once_then_applies_cleaned_text_and_speakers(
    tmp_path, monkeypatch
):
    analysis = tmp_path / "analysis"
    (analysis / "transcripts").mkdir(parents=True)
    source_url = "https://kick.com/example/videos/recording"
    (analysis / "manifest.json").write_text(
        json.dumps({"source_url": source_url, "analysis_key": "key", "duration": 1800})
    )
    (analysis / "asr-receipt.json").write_text(json.dumps({"key": "saved"}))
    clips, segments = [], []
    for i in range(5):
        start = i * 330
        clips.append(
            {
                "start": start,
                "end": start + 300,
                "title": f"話題{i}",
                "reason": "r",
                "confidence": 0.9,
            }
        )
        words = [
            Word(start=start + 5, end=start + 6, text="えー"),
            Word(start=start + 6, end=start + 7, text="南"),
            Word(start=start + 7, end=start + 10, text="くんが来ます。"),
        ]
        segments.append(
            Segment(
                start=start + 5,
                end=start + 10,
                text="えー南くんが来ます。",
                words=words,
            )
        )
    (analysis / "transcripts" / "saved.json").write_text(
        Transcript(language="ja", segments=segments).model_dump_json()
    )
    config = tmp_path / "config.json"
    config.write_text(Settings(aspect="16:9", height=360).model_dump_json())
    url = "https://kick.com/example"
    research = {
        "terms": [
            {
                "surface": "南",
                "preferred": "ミナミ",
                "source_urls": [url],
                "evidence": "本人の活動名",
                "confidence": 0.95,
            }
        ]
    }
    careful = {
        "audio_access": "heard",
        "segments": [
            {"segment_id": f"c{i:02}s0001", "text": "ミナミくんが来ます。"}
            for i in range(1, 6)
        ],
        "reviewed_clip_ids": [1, 2, 3, 4, 5],
        "reviewed_fast_segment_ids": [],
        "uncertain_segment_ids": [],
        "notes": [],
        "speaker_assignments": [
            {
                "segment_id": f"c{i:02}s0001",
                "speaker_id": "speaker_2",
                "confidence": 0.95,
            }
            for i in range(1, 6)
        ],
    }
    requests = []

    class Response:
        status = 200
        headers = {}

        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.value).encode()

    def urlopen(request, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            value = {"editorial_brief": "まとめ", "clips": clips}
        else:
            value = research
        candidate = {
            "finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps(value)}]},
        }
        if len(requests) == 2:
            candidate["groundingMetadata"] = {
                "groundingChunks": [{"web": {"uri": url}}],
                "groundingSupports": [{"groundingChunkIndices": [0]}],
            }
        return Response({"candidates": [candidate]})

    monkeypatch.setenv("CLIPFORGE_VERTEX_ACCESS_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(gemini_audio_editor.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        gemini_audio_editor,
        "prepare_selected_audio",
        lambda source, clips, target, gap_seconds: target.write_bytes(b"synthetic"),
    )
    monkeypatch.setattr(
        gemini_audio_editor, "inspect_audio_input", lambda *args: {"synthetic": True}
    )
    monkeypatch.setattr(
        gemini_audio_editor,
        "collect_sse_response",
        lambda *args: (
            {"candidates": [{"finishReason": "STOP"}]},
            json.dumps(careful),
            1,
            1,
        ),
    )
    output = tmp_path / "out"
    gemini_audio_editor.run(
        SimpleNamespace(
            analysis=analysis,
            output=output,
            config=config,
            asr_first=True,
            resume_from=None,
        )
    )
    assert len(requests) == 3
    assert requests[1]["tools"] == [{"googleSearch": {}}]
    prompt = requests[2]["contents"][0]["parts"][0]["text"]
    assert (
        "ケバ取り" in prompt and "ミナミ" in prompt and "speaker_assignments" in prompt
    )
    final = json.loads((output / "final-selection.json").read_text())
    for original, clip in zip(segments, final["clips"]):
        updated = audio_first.edited_transcript(
            Transcript(language="ja", segments=[original]),
            clip["start"],
            clip["caption_edits"],
        )
        assert updated.segments[0].text == "ミナミくんが来ます。"
        assert updated.segments[0].words[-1] == original.words[-1]
        assert clip["speaker_turns"][0]["speaker_id"] == "speaker_2"
    # Replay an already received result without a fresh model/audio call.
    monkeypatch.setattr(
        gemini_audio_editor.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("replay called Vertex"),
    )
    monkeypatch.setattr(
        gemini_audio_editor,
        "prepare_selected_audio",
        lambda *a, **k: pytest.fail("replay encoded audio"),
    )
    replay = tmp_path / "replay"
    gemini_audio_editor.run(
        SimpleNamespace(
            analysis=analysis,
            output=replay,
            config=config,
            asr_first=True,
            resume_from=output,
        )
    )
    assert json.loads((replay / "final-selection.json").read_text()) == final
    assert (
        json.loads((replay / "audio-editor-receipt.json").read_text())[
            "new_generation_requests"
        ]
        == 0
    )
