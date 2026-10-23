import json
import math

import pysubs2
import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from clipforge import auto_layout, captions, pipeline, signals, silence
from clipforge.models import (
    Diagram,
    Segment,
    SelectedClip,
    Settings,
    Transcript,
    Visual,
    Word,
    Zoom,
)
from clipforge.storage import PipelineError, digest, fingerprint


class FixedWidth:
    """Ten pixels per character, so breaks are predictable without a font."""

    def width(self, text):
        return 10 * len(text)


def make(**overrides):
    return Settings(
        **{
            "gemini_model": "manual-no-api",
            "min_seconds": 3,
            "max_seconds": 30,
            **overrides,
        }
    )


def png(path, size=(40, 30)):
    Image.new("RGB", size, (200, 40, 40)).save(path, format="PNG")
    return path


# --- captions -------------------------------------------------------------


def test_line_breaks_are_inserted_before_the_given_characters():
    assert captions.with_breaks("abcdef", {2, 4}) == "ab\\Ncd\\Nef"
    assert captions.safe("{x}\\y\r\n") == "｛x｝＼y  "


def test_karaoke_and_sequential_modes_tag_each_token():
    tokens = [(0.0, 0.5, "今日は"), (0.5, 1.0, "晴れ")]
    karaoke = captions.group_text(tokens, FixedWidth(), 500, "karaoke")
    assert karaoke == "{\\kf50}今日は{\\kf50}晴れ"
    sequential = captions.group_text(tokens, FixedWidth(), 500, "sequential")
    assert sequential.startswith("{\\k50}今日は{\\k50}晴れ")
    assert captions.group_text(tokens, FixedWidth(), 500, "plain") == "今日は晴れ"


def test_long_tokens_are_split_and_groups_break_on_pauses_and_length():
    split, count = captions.split_long([(0.0, 4.0, "abcdefgh")], FixedWidth(), 25)
    assert count == 1
    assert [text for _, _, text in split] == ["ab", "cd", "ef", "gh"]
    assert split[0][0] == 0.0 and split[-1][1] == pytest.approx(4.0)
    tokens = [(0.0, 1.0, "一"), (1.0, 2.0, "二"), (3.5, 4.0, "三"), (9.0, 9.5, "四")]
    groups = captions.group(tokens, FixedWidth(), 500)
    assert [[t[2] for t in g] for g in groups] == [["一", "二"], ["三"], ["四"]]


def test_wrapping_prefers_phrase_edges_and_respects_the_line_limit():
    assert captions.wrap_breaks("短い", FixedWidth(), 100) == []
    breaks = captions.wrap_breaks("日本語字幕の表示と時刻を", FixedWidth(), 70)
    assert breaks and all(0 < b < len("日本語字幕の表示と時刻を") for b in breaks)


def test_collect_tokens_divides_a_word_cut_by_silence_removal():
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(
                start=9,
                end=13,
                text="abcdefgh",
                words=[Word(start=9, end=13, text="abcdefgh")],
            )
        ],
    )
    clip = SelectedClip(
        start=10, end=14, candidate_id="m", title="t", reason="r", confidence=1
    )
    tokens, approximate = captions.collect_tokens(
        transcript, clip, [(10.0, 11.0), (12.0, 14.0)]
    )
    # Both kept halves last one second, so the eight characters divide evenly between them.
    assert approximate == 1
    assert [(start, end, text) for start, end, text in tokens] == [
        (0.0, 1.0, "abcd"),
        (1.0, 2.0, "efgh"),
    ]


def _collect(words, language):
    """Tokens from (start, end, text) words, all in one segment, with no silence cuts."""
    transcript = Transcript(
        language=language,
        segments=[
            Segment(
                start=words[0][0],
                end=words[-1][1],
                text="".join(text for _, _, text in words),
                words=[Word(start=a, end=b, text=t) for a, b, t in words],
            )
        ],
    )
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="t", reason="r", confidence=1
    )
    tokens, _ = captions.collect_tokens(transcript, clip, None)
    return tokens


def test_japanese_marks_become_fullwidth_and_a_space_follows_before_more_text():
    tokens = _collect(
        [(0, 1, "本当?"), (1, 2, "すごい!"), (2, 3, "ね?")], language="ja"
    )
    assert tokens == [(0, 1, "本当？　"), (1, 2, "すごい！　"), (2, 3, "ね？")]


@pytest.mark.parametrize("closer", list("」』）)］】〉》、。…‥？！"))
def test_no_space_is_added_before_a_closing_mark(closer):
    tokens = _collect([(0, 1, "あ!"), (1, 2, closer)], language="ja")
    assert tokens == [(0, 1, "あ！"), (1, 2, closer)]


def test_a_space_is_added_after_a_mark_when_the_next_token_continues():
    tokens = _collect([(0, 1, "えっ?"), (1, 2, "まじ")], language="ja")
    assert tokens[0] == (0, 1, "えっ？　")
    tokens = _collect([(0, 1, "やった!"), (1, 2, "」")], language="ja")
    assert tokens == [(0, 1, "やった！"), (1, 2, "」")]


def test_a_leading_space_of_the_next_token_is_replaced_by_the_fullwidth_space():
    # Without this, "え？　 次" would show two gaps; the next token keeps its timing.
    tokens = _collect([(0, 1, "え?"), (1, 2, " 次")], language="ja")
    assert tokens == [(0, 1, "え？　"), (1, 2, "次")]


def test_a_trailing_space_before_the_fullwidth_space_is_replaced():
    tokens = _collect([(0, 1, "え? "), (1, 2, "次")], language="ja")
    assert tokens == [(0, 1, "え？　"), (1, 2, "次")]


def test_a_mark_already_followed_by_a_fullwidth_space_is_left_alone():
    tokens = _collect([(0, 1, "え？　"), (1, 2, "次")], language="ja")
    assert tokens == [(0, 1, "え？　"), (1, 2, "次")]
    tokens = _collect([(0, 1, "え?　"), (1, 2, "次")], language="ja")
    assert tokens == [(0, 1, "え？　"), (1, 2, "次")]


def test_marks_outside_japanese_clips_are_left_alone():
    tokens = _collect([(0, 1, "Wow!"), (1, 2, "really?")], language="en")
    assert tokens == [(0, 1, "Wow!"), (1, 2, "really?")]


def test_mark_conversion_keeps_the_timing_of_every_token():
    words = [(0, 1.5, "本当?"), (1.5, 3, "すごい!"), (3, 4, "ね")]
    ja = _collect(words, language="ja")
    assert [(start, end) for start, end, _ in ja] == [(0, 1.5), (1.5, 3), (3, 4)]


def test_heading_shrinks_then_refuses_titles_that_cannot_fit(japanese_font):
    assert captions.heading_size("短い題名", 404, 720, japanese_font) <= round(
        720 * 0.07
    )
    with pytest.raises(PipelineError, match="does not fit"):
        captions.heading_size("とても長い題名" * 30, 404, 720, japanese_font)


def test_heading_scale_enlarges_the_heading(japanese_font):
    base = captions.heading_size("短い題名", 404, 720, japanese_font)
    scaled = captions.heading_size("短い題名", 404, 720, japanese_font, 1.4)
    assert scaled > base


def test_heading_align_left_pins_the_heading_to_the_top_left(
    tmp_path, monkeypatch, japanese_font, transcript
):
    monkeypatch.setattr(captions.fonts, "resolve", lambda family: japanese_font)
    clip = SelectedClip(
        start=0, end=30, candidate_id="m", title="テスト題名", reason="r", confidence=1
    )
    captions.create(
        transcript, clip, tmp_path, make(heading=True, heading_align="left")
    )
    subtitles = pysubs2.load(str(tmp_path / "captions.ass"))
    assert subtitles.styles["Heading"].alignment == pysubs2.Alignment.TOP_LEFT


def test_heading_and_credits_stay_out_of_the_srt_and_wrap_inside_the_frame(
    tmp_path, monkeypatch, japanese_font, transcript
):
    monkeypatch.setattr(captions.fonts, "resolve", lambda family: japanese_font)
    clip = SelectedClip(
        start=0, end=30, candidate_id="m", title="テスト題名", reason="r", confidence=1
    )
    credits = [
        "とても長い作者の名前 / CC BY-SA 4.0 / Wikimedia Commons",
        "Another author / CC0 / Openverse",
    ]
    result = captions.create(
        transcript, clip, tmp_path, make(heading=True), credits=credits
    )
    ass = (tmp_path / "captions.ass").read_text()
    assert "Heading" in ass and "テスト題名" in ass
    credit_line = next(line for line in ass.splitlines() if ",Credit," in line)
    assert "\\N" in credit_line
    srt = (tmp_path / "captions.srt").read_text()
    assert "テスト題名" not in srt and "Wikimedia" not in srt
    assert result["heading"] is True and result["caption_mode"] == "karaoke"


def test_sequential_captions_use_the_sequential_style(
    tmp_path, monkeypatch, japanese_font, transcript
):
    monkeypatch.setattr(captions.fonts, "resolve", lambda family: japanese_font)
    clip = SelectedClip(
        start=0, end=30, candidate_id="m", title="t", reason="r", confidence=1
    )
    captions.create(transcript, clip, tmp_path, make(caption="sequential"))
    assert ",Sequential," in (tmp_path / "captions.ass").read_text()


def test_caption_parser_is_loaded_once():
    assert captions.parser() is captions.parser()


# --- signals --------------------------------------------------------------


def dense_transcript(duration):
    return Transcript(
        language="ja",
        segments=[
            Segment(start=s, end=s + 10, text="同じ話題の発話です")
            for s in range(0, int(duration) - 10, 12)
        ],
    )


def test_candidates_reach_late_topics_when_topics_are_given(settings):
    settings.candidates = 4
    settings.clips = 2
    spans = [(0, 100), (100, 200), (200, 300)]
    raw = signals.candidates(
        {"duration": 300},
        dense_transcript(300),
        {"boundaries": []},
        {"events": [], "status": "not_requested"},
        settings,
        topics=spans,
    )
    middles = [(c["start"] + c["end"]) / 2 for c in raw]
    assert len(raw) == 4
    assert min(middles) < 100 and max(middles) >= 200


def test_candidates_without_topics_spread_over_the_video(settings):
    settings.candidates = 4
    raw = signals.candidates(
        {"duration": 300},
        dense_transcript(300),
        {"boundaries": []},
        {"events": [], "status": "not_requested"},
        settings,
    )
    middles = [(c["start"] + c["end"]) / 2 for c in raw]
    assert min(middles) < 100 and max(middles) >= 200


# --- pipeline helpers -----------------------------------------------------


def test_receipt_accepts_only_its_own_untouched_files(tmp_path):
    target = tmp_path / "clip-001"
    target.mkdir()
    (target / "visuals").mkdir()
    names = {
        "video.mp4": b"video",
        "captions.ass": b"ass",
        "captions.srt": b"srt",
        "ledger.json": json.dumps([{"file": "visual-01.png"}]).encode(),
        "quality.json": b"{}",
        "visuals/visual-01.png": b"png",
    }
    for name, data in names.items():
        (target / name).write_bytes(data)
    ledger = [{"file": "visual-01.png"}]
    saved = {
        "key": "k",
        "ledger": ledger,
        "sha256": {name: digest(target / name) for name in names},
    }
    assert pipeline.valid_receipt(target, saved, "k")
    assert not pipeline.valid_receipt(target, saved, "other-job")
    (target / "visuals/visual-01.png").write_bytes(b"changed")
    assert not pipeline.valid_receipt(target, saved, "k")
    assert not pipeline.valid_receipt(
        target, {**saved, "ledger": [{"file": "../x.png"}]}, "k"
    )
    # An edited ledger in render.json alone must not be accepted on resume.
    assert not pipeline.valid_receipt(
        target,
        {**saved, "ledger": [{"file": "visual-01.png", "source_credit": "Bob"}]},
        "k",
    )
    extra = {
        **saved,
        "sha256": {**saved["sha256"], "notes.txt": digest(target / "ledger.json")},
    }
    assert not pipeline.valid_receipt(target, extra, "k")
    assert not pipeline.valid_receipt(target, {"key": "k"}, "k")
    assert pipeline.visual_names(ledger) == {"visuals/visual-01.png"}


def test_parallel_results_keep_order_and_failure_stops_new_work():
    assert pipeline.run_parallel(lambda n: n * n, [1, 2, 3, 4], 3) == [1, 4, 9, 16]
    started = []

    def fails_second(item):
        started.append(item)
        if item == 2:
            raise PipelineError("second failed")
        return item

    with pytest.raises(PipelineError, match="second failed"):
        pipeline.run_parallel(fails_second, [1, 2, 3, 4], 1)
    assert started == [1, 2]


def test_cut_regions_follow_the_silence_setting(transcript, monkeypatch):
    clip = SelectedClip(
        start=0, end=30, candidate_id="m", title="t", reason="r", confidence=1
    )
    assert pipeline.cut_regions("/in.mp4", transcript, clip, make()) is None
    keep = pipeline.cut_regions("/in.mp4", transcript, clip, make(silence_cut=True))
    assert keep and all(0 <= a < b <= 30 for a, b in keep)
    empty = Transcript(language="ja", segments=[])
    assert pipeline.cut_regions("/in.mp4", empty, clip, make(silence_cut=True)) is None
    monkeypatch.setattr(
        silence, "silero_spans", lambda source, start, end: [(1.0, 2.0)]
    )
    assert pipeline.cut_regions(
        "/in.mp4", transcript, clip, make(silence_cut=True, vad="silero")
    ) == [(0.85, 2.15)]


def test_prepare_visuals_copies_supplied_images_and_converts_svg(tmp_path):
    card = png(tmp_path / "card.png")
    vector = tmp_path / "shape.svg"
    vector.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect width="40" height="20" fill="#333"/></svg>'
    )
    settings = make(
        visuals=[
            Visual(
                file=str(card),
                start=1,
                end=3,
                source_credit="作者 / CC0",
                source_url="https://e/c",
                license="CC0",
            ),
            Visual(
                file=str(vector),
                start=4,
                end=6,
                source_credit="SVG",
                source_url="https://e/s",
                license="CC0",
            ),
        ]
    )
    folder = tmp_path / "clip"
    folder.mkdir()
    visuals, ledger = pipeline.prepare_visuals(
        SelectedClip(
            start=0, end=10, candidate_id="m", title="t", reason="r", confidence=1
        ),
        settings,
        folder,
        10.0,
    )
    assert [e["file"] for e in ledger] == ["visual-01.png", "visual-02.png"]
    assert all(e["kind"] == "supplied" for e in ledger)
    assert all((folder / "visuals" / e["file"]).is_file() for e in ledger)
    assert visuals[0]["position"] == "right" and visuals[1]["file"].endswith(
        "visual-02.png"
    )


@pytest.mark.parametrize(
    ("visual", "message"),
    [
        (Visual(file="/x.png", start=0, end=1), "needs source_credit"),
        (
            Visual(
                file="/does/not/exist.png",
                start=0,
                end=1,
                source_credit="a",
                source_url="u",
                license="CC0",
            ),
            "visual file is missing",
        ),
    ],
)
def test_supplied_visuals_need_a_complete_source(tmp_path, visual, message):
    with pytest.raises(PipelineError, match=message):
        pipeline.prepare_visuals(
            SelectedClip(
                start=0, end=10, candidate_id="m", title="t", reason="r", confidence=1
            ),
            make(visuals=[visual]),
            tmp_path,
            10.0,
        )


def test_unsupported_visual_formats_are_refused(tmp_path):
    gif = tmp_path / "anim.gif"
    gif.write_bytes(b"GIF89a")
    settings = make(
        visuals=[
            Visual(
                file=str(gif),
                start=0,
                end=1,
                source_credit="a",
                source_url="u",
                license="CC0",
            )
        ]
    )
    with pytest.raises(PipelineError, match="SVG, PNG, JPEG or WebP"):
        pipeline.prepare_visuals(
            SelectedClip(
                start=0, end=10, candidate_id="m", title="t", reason="r", confidence=1
            ),
            settings,
            tmp_path,
            10.0,
        )


def test_diagrams_and_searched_images_are_recorded_in_the_ledger(
    tmp_path, monkeypatch, japanese_font
):
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    entry = {
        "id": "image-01",
        "file": "image-01.png",
        "kind": "photo",
        "sha256": "0" * 64,
        "provider": "wikimedia-commons",
        "source_url": "https://e/i",
        "source_credit": "A / CC0",
        "license": "CC0",
        "license_url": "",
        "query": "q",
        "retrieved_at": "2026-10-21T00:00:00+00:00",
    }
    monkeypatch.setattr(
        pipeline.assets, "search", lambda query, sources, limit: [{"image_url": "u"}]
    )
    monkeypatch.setattr(
        pipeline.assets, "download_all", lambda items, directory: [entry]
    )
    settings = make(
        diagrams=[
            Diagram(kind="steps", title="手順", start=1, end=2, steps=["一", "二"])
        ],
        image_search=True,
    )
    visuals, ledger = pipeline.prepare_visuals(
        SelectedClip(
            start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
        ),
        settings,
        tmp_path,
        10.0,
    )
    kinds = {e["id"]: e["kind"] for e in ledger}
    assert kinds == {"diagram-01": "diagram", "image-01": "photo"}
    searched = next(v for v in visuals if v["file"].endswith("image-01.png"))
    assert searched["start"] == 0 and searched["end"] == 10
    assert (tmp_path / "visuals" / "diagram-01.png").is_file()


def test_clip_directory_lists_its_visuals_and_skips_diagram_credits(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    source = tmp_path / "in.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "out" / "clips"
    root.mkdir(parents=True)
    card = png(tmp_path / "card.png")
    settings = make(
        visuals=[
            Visual(
                file=str(card),
                start=1,
                end=3,
                source_credit="作者 / CC0",
                source_url="https://e/c",
                license="CC0",
            )
        ],
        diagrams=[Diagram(kind="steps", title="手順", start=4, end=6, steps=["一"])],
        thumbnail=True,
    )
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
    )
    seen = {}

    def fake_render(
        source, destination, clip, settings, ass=None, keep=None, visuals=()
    ):
        destination.write_bytes(b"video")
        return {
            "duration": 10,
            "width": 404,
            "height": 720,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    def fake_captions(
        transcript, clip, directory, settings, keep=None, credits=None, lifts=None
    ):
        seen["credits"] = credits
        pysubs2.SSAFile().save(str(directory / "captions.ass"))
        pysubs2.SSAFile().save(str(directory / "captions.srt"))
        return {"events": 0, "heading": False, "missing_glyphs": []}

    def fake_thumbnail(video, destination, title, font_path, at_seconds):
        destination.write_bytes(b"thumb")

    monkeypatch.setattr(pipeline.media, "render", fake_render)
    monkeypatch.setattr(pipeline, "create_captions", fake_captions)
    monkeypatch.setattr(pipeline.thumbnail, "make", fake_thumbnail)
    result = pipeline.render_clip(
        source, root, 1, clip, transcript, settings, "job-key"
    )
    assert seen["credits"] == ["作者 / CC0"]
    assert result["quality"]["passed"] is True
    assert set(result["sha256"]) == {
        "captions.ass",
        "captions.srt",
        "ledger.json",
        "quality.json",
        "thumbnail.png",
        "video.mp4",
        "visuals/visual-01.png",
        "visuals/diagram-01.png",
    }
    target = root / "clip-001"
    saved = json.loads((target / "render.json").read_text())
    assert pipeline.valid_receipt(target, saved, saved["key"])

    def must_not_render(*args, **kwargs):
        raise AssertionError("a verified clip must be reused, not rendered again")

    monkeypatch.setattr(pipeline.media, "render", must_not_render)
    again = pipeline.render_clip(source, root, 1, clip, transcript, settings, "job-key")
    assert again["sha256"] == result["sha256"]
    (target / "visuals" / "visual-01.png").write_bytes(b"tampered")
    with pytest.raises(PipelineError, match="differs from the saved render receipt"):
        pipeline.render_clip(source, root, 1, clip, transcript, settings, "job-key")


def test_existing_clip_without_receipt_is_not_trusted(tmp_path, transcript):
    root = tmp_path / "clips"
    (root / "clip-001").mkdir(parents=True)
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="t", reason="r", confidence=1
    )
    with pytest.raises(PipelineError, match="no verified render receipt"):
        pipeline.render_clip(
            tmp_path / "in.mp4", root, 1, clip, transcript, make(), "k"
        )
    key = fingerprint(
        {"job": "k", "clip": clip.model_dump(), "transcript": transcript.model_dump()}
    )
    assert key


def _chat_clip_render(tmp_path, monkeypatch, japanese_font, transcript, chat, layout):
    """Renders clip 1 with a chat panel and a fixed layout; media and planning are synthetic."""
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    source = tmp_path / "in.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "clips"
    root.mkdir()
    settings = make(aspect="16:9", height=1080, chat_panel=True)
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
    )

    def fake_render(source, destination, clip, settings, **options):
        destination.write_bytes(b"video")
        return {
            "duration": 10,
            "width": 1920,
            "height": 1080,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    def fake_captions(
        transcript, clip, directory, settings, keep=None, credits=None, lifts=None
    ):
        pysubs2.SSAFile().save(str(directory / "captions.ass"))
        pysubs2.SSAFile().save(str(directory / "captions.srt"))
        return {"events": 0, "heading": False, "missing_glyphs": []}

    monkeypatch.setattr(pipeline, "cut_regions", lambda *args: [])
    monkeypatch.setattr(pipeline, "landscape_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(pipeline.media, "render", fake_render)
    monkeypatch.setattr(pipeline, "create_captions", fake_captions)
    result = pipeline.render_clip(
        source, root, 1, clip, transcript, settings, "job", chat=chat
    )
    return source, root, settings, clip, result


def _forbidden(*args, **kwargs):
    raise AssertionError("a rendered clip must not be planned or rendered again")


def test_rerun_returns_a_rendered_clip_without_planning_or_fetching_again(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    chat = {
        "ass": "[Script Info]\nPlayResX: 1920\n",
        "messages": 0,
        "reason": None,
        "error": None,
    }
    layout = {
        "anchor": "right",
        "requested": "auto",
        "x": 0.25,
        "lifts": [0.2, 0.12],
        "static": None,
        "reason": None,
    }
    source, root, settings, clip, first = _chat_clip_render(
        tmp_path, monkeypatch, japanese_font, transcript, chat, layout
    )
    receipt = root / "clip-001" / "render.json"
    assert json.loads(receipt.read_text())["render_inputs"] == {
        "chat": {"ass_sha256": fingerprint(chat["ass"]), "reason": None},
        "layout": {"x": 0.25, "lifts": [0.2, 0.12], "requested": "auto"},
    }
    monkeypatch.setattr(pipeline, "cut_regions", _forbidden)
    monkeypatch.setattr(pipeline, "landscape_layout", _forbidden)
    monkeypatch.setattr(pipeline.media, "render", _forbidden)
    again = pipeline.render_clip(
        source,
        root,
        1,
        clip,
        transcript,
        make(aspect="16:9", height=1080, chat_panel=True),
        "job",
    )
    assert again["sha256"] == first["sha256"]
    assert again["directory"] == "clips/clip-001"
    # The chat was read again and came out different; the stored render still stands.
    changed = dict(chat, ass="[Script Info]\nPlayResX: 1280\n")
    again = pipeline.render_clip(
        source,
        root,
        1,
        clip,
        transcript,
        make(aspect="16:9", height=1080, chat_panel=True),
        "job",
        chat=changed,
    )
    assert again["sha256"] == first["sha256"]
    with pytest.raises(PipelineError, match="differs from the saved render receipt"):
        pipeline.render_clip(
            source,
            root,
            1,
            clip,
            transcript,
            make(aspect="16:9", height=1080, chat_panel=True),
            "other-job",
        )
    (root / "clip-001" / "video.mp4").write_bytes(b"tampered")
    with pytest.raises(PipelineError, match="differs from the saved render receipt"):
        pipeline.render_clip(
            source,
            root,
            1,
            clip,
            transcript,
            make(aspect="16:9", height=1080, chat_panel=True),
            "job",
        )


def test_rerun_refuses_another_crop_side_or_caption_floor(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    chat = {
        "ass": "[Script Info]\nPlayResX: 1920\n",
        "messages": 0,
        "reason": None,
        "error": None,
    }
    layout = {
        "anchor": "right",
        "requested": "right",
        "x": 0.25,
        "lifts": [0.2, 0.12],
        "floor": 0.1,
        "static": None,
        "reason": None,
    }
    source, root, _, clip, first = _chat_clip_render(
        tmp_path, monkeypatch, japanese_font, transcript, chat, layout
    )
    monkeypatch.setattr(pipeline, "cut_regions", _forbidden)
    monkeypatch.setattr(pipeline, "landscape_layout", _forbidden)
    settings = make(aspect="16:9", height=1080, chat_panel=True)
    again = pipeline.render_clip(
        source,
        root,
        1,
        clip,
        transcript,
        settings,
        "job",
        chat=None,
        crop_anchor="right",
        caption_floor=0.1,
    )
    assert again["sha256"] == first["sha256"]
    for crop_anchor, floor in (("left", 0.1), ("right", None), ("right", 0.2)):
        with pytest.raises(
            PipelineError, match="differs from the saved render receipt"
        ):
            pipeline.render_clip(
                source,
                root,
                1,
                clip,
                transcript,
                settings,
                "job",
                chat=None,
                crop_anchor=crop_anchor,
                caption_floor=floor,
            )


def _plain_clip(tmp_path, monkeypatch, japanese_font, transcript, settings):
    """Renders clip 1 of a 16:9 render without chat; media and captions are synthetic."""
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    source = tmp_path / "in.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "plain"
    root.mkdir()
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
    )

    def fake_render(source, destination, clip, settings, **options):
        destination.write_bytes(b"video")
        return {
            "duration": 10,
            "width": 1920,
            "height": 1080,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    def fake_captions(
        transcript, clip, directory, settings, keep=None, credits=None, lifts=None
    ):
        pysubs2.SSAFile().save(str(directory / "captions.ass"))
        pysubs2.SSAFile().save(str(directory / "captions.srt"))
        return {"events": 0, "heading": False, "missing_glyphs": []}

    monkeypatch.setattr(pipeline, "cut_regions", lambda *args: [])
    monkeypatch.setattr(pipeline.media, "render", fake_render)
    monkeypatch.setattr(pipeline, "create_captions", fake_captions)
    result = pipeline.render_clip(source, root, 1, clip, transcript, settings, "job")
    return source, root, clip, result


def test_rerun_refuses_a_layout_the_render_did_not_have(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    settings = make(aspect="16:9", height=1080, caption_lift=False)
    source, root, clip, first = _plain_clip(
        tmp_path, monkeypatch, japanese_font, transcript, settings
    )
    stored = json.loads((root / "clip-001" / "render.json").read_text())
    assert stored["render_inputs"] == {"chat": None, "layout": None}
    monkeypatch.setattr(pipeline, "landscape_layout", _forbidden)
    monkeypatch.setattr(pipeline.media, "render", _forbidden)
    again = pipeline.render_clip(source, root, 1, clip, transcript, settings, "job")
    assert again["sha256"] == first["sha256"]
    # The crop side has no effect without the chat, so it is not compared.
    again = pipeline.render_clip(
        source, root, 1, clip, transcript, settings, "job", crop_anchor="left"
    )
    assert again["sha256"] == first["sha256"]
    lifted = make(aspect="16:9", height=1080, caption_lift=True)
    with pytest.raises(PipelineError, match="differs from the saved render receipt"):
        pipeline.render_clip(source, root, 1, clip, transcript, lifted, "job")
    with pytest.raises(PipelineError, match="differs from the saved render receipt"):
        pipeline.render_clip(
            source, root, 1, clip, transcript, settings, "job", caption_floor=0.1
        )


def test_old_receipt_without_layout_refuses_a_layout_now_asked_for(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    settings = make(aspect="16:9", height=1080, caption_lift=False)
    source, root, clip, first = _plain_clip(
        tmp_path, monkeypatch, japanese_font, transcript, settings
    )
    receipt = root / "clip-001" / "render.json"
    stored = json.loads(receipt.read_text())
    del stored["render_inputs"]
    receipt.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(pipeline, "cut_regions", _forbidden)
    monkeypatch.setattr(pipeline.media, "render", _forbidden)
    again = pipeline.render_clip(source, root, 1, clip, transcript, settings, "job")
    assert again["sha256"] == first["sha256"]
    lifted = make(aspect="16:9", height=1080, caption_lift=True)
    with pytest.raises(PipelineError, match="differs from the saved render receipt"):
        pipeline.render_clip(source, root, 1, clip, transcript, lifted, "job")


def test_render_key_is_the_key_earlier_receipts_were_made_with(
    transcript,
):
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
    )
    chat_inputs = {"ass_sha256": fingerprint("ASS"), "reason": "chat_unavailable"}
    layout_inputs = {"x": 0.25, "lifts": [0.2], "requested": "right", "floor": 0.1}
    earlier = fingerprint(
        {
            "job": "job",
            "clip": clip.model_dump(),
            "transcript": transcript.model_dump(),
            "chat": {"ass": fingerprint("ASS"), "reason": "chat_unavailable"},
            "layout": {
                "x": 0.25,
                "lifts": [0.2],
                "requested": "right",
                "floor": 0.1,
            },
        }
    )
    key = pipeline._render_key("job", clip, transcript, chat_inputs, layout_inputs)
    assert key == earlier
    plain = fingerprint(
        {
            "job": "job",
            "clip": clip.model_dump(),
            "transcript": transcript.model_dump(),
        }
    )
    assert pipeline._render_key("job", clip, transcript, None, None) == plain


def test_receipt_without_saved_inputs_is_matched_as_before(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    chat = {
        "ass": "[Script Info]\nPlayResX: 1920\n",
        "messages": 0,
        "reason": None,
        "error": None,
    }
    layout = {
        "anchor": "right",
        "requested": "auto",
        "x": 0.25,
        "lifts": [0.2, 0.12],
        "static": None,
        "reason": None,
    }
    source, root, settings, clip, first = _chat_clip_render(
        tmp_path, monkeypatch, japanese_font, transcript, chat, layout
    )
    receipt = root / "clip-001" / "render.json"
    stored = json.loads(receipt.read_text())
    del stored["render_inputs"]
    receipt.write_text(json.dumps(stored), encoding="utf-8")
    # Without saved inputs the clip is planned again; the same plan and chat give the same key.
    monkeypatch.setattr(pipeline, "cut_regions", lambda *args: [])
    monkeypatch.setattr(pipeline, "landscape_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(pipeline.media, "render", _forbidden)
    again = pipeline.render_clip(
        source, root, 1, clip, transcript, settings, "job", chat=chat
    )
    assert again["sha256"] == first["sha256"]
    # A receipt of a plain render (no chat, no layout) is matched without any analysis.
    plain = make()
    plain_root = tmp_path / "plain"
    plain_root.mkdir()

    def plain_render(source, destination, clip, settings, **options):
        destination.write_bytes(b"video")
        return {
            "duration": 10,
            "width": 404,
            "height": 720,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    monkeypatch.setattr(pipeline.media, "render", plain_render)

    def plain_captions(
        transcript, clip, directory, settings, keep=None, credits=None, lifts=None
    ):
        pysubs2.SSAFile().save(str(directory / "captions.ass"))
        pysubs2.SSAFile().save(str(directory / "captions.srt"))
        return {"events": 0, "heading": False, "missing_glyphs": []}

    monkeypatch.setattr(pipeline, "create_captions", plain_captions)
    monkeypatch.setattr(pipeline, "landscape_layout", lambda *args, **kwargs: None)
    rendered = pipeline.render_clip(
        source, plain_root, 1, clip, transcript, plain, "job"
    )
    stored = json.loads((plain_root / "clip-001" / "render.json").read_text())
    assert stored["render_inputs"] == {"chat": None, "layout": None}
    del stored["render_inputs"]
    (plain_root / "clip-001" / "render.json").write_text(
        json.dumps(stored), encoding="utf-8"
    )
    monkeypatch.setattr(pipeline, "landscape_layout", _forbidden)
    monkeypatch.setattr(pipeline, "cut_regions", _forbidden)
    again = pipeline.render_clip(source, plain_root, 1, clip, transcript, plain, "job")
    assert again["sha256"] == rendered["sha256"]


# --- models ---------------------------------------------------------------


def test_zoom_visual_and_diagram_validate_their_intervals():
    with pytest.raises(ValidationError):
        Zoom(start=3, end=2)
    with pytest.raises(ValidationError):
        Visual(file="x.png", start=3, end=3)
    with pytest.raises(ValidationError):
        Diagram(kind="table", title="t", start=0, end=1, rows=[["a"], ["b", "c"]])
    with pytest.raises(ValidationError):
        Diagram(kind="bars", title="t", start=0, end=1, labels=["a"], values=[])
    with pytest.raises(ValidationError):
        Diagram(kind="steps", title="t", start=0, end=1)
    with pytest.raises(ValidationError):
        Diagram(kind="table", title="t", start=2, end=1, rows=[["a"]])


@pytest.mark.parametrize(
    "update",
    [
        {"layout": "panels"},
        {"zoom": [Zoom(start=0, end=1)]},
        {"candidates": 1, "clips": 2},
        {"gemini_model": "bad model name"},
        {"font": "Noto, Sans"},
        {"face_box": (0.9, 0.9, 0.5, 0.5)},
        {"min_seconds": 40, "max_seconds": 30},
        {"topic_min_seconds": 100, "topic_max_seconds": 120},
        {"analysis_fps": 5, "max_seconds": 180, "candidates": 10},
    ],
)
def test_settings_reject_inconsistent_options(update):
    with pytest.raises(ValidationError):
        make(**update)


def test_settings_accept_a_consistent_panel_layout():
    settings = make(
        layout="panels", face_box=(0.1, 0.1, 0.4, 0.4), game_box=(0, 0, 1, 1)
    )
    assert settings.layout == "panels" and math.isclose(settings.face_box[2], 0.4)


def test_numbers_with_their_units_never_split_across_lines():
    text = "あいう1,234万円えお"
    # "1,234万円" is 70 px here, so it fits on an 80 px line and must not be broken.
    breaks = captions.wrap_breaks(text, FixedWidth(), 80)
    assert breaks and not any(4 <= b <= 9 for b in breaks)
    # A number wider than the whole line has to be split somewhere.
    assert captions.wrap_breaks("1,234万円", FixedWidth(), 60)


def test_table_query_adds_a_sourced_table_overlay(tmp_path, monkeypatch, japanese_font):
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    found = [
        {
            "title": "日本の人口",
            "rows": [["年", "人口"], ["2020", "1億2千万"], ["2021", "1億2千万"]],
            "source_url": "https://ja.wikipedia.org/wiki/x",
            "source_credit": "日本語版Wikipedia「日本の人口」の表 / CC BY-SA 4.0",
            "license": "CC BY-SA 4.0",
        }
    ]
    monkeypatch.setattr(pipeline.tables, "search", lambda query, limit=3: found)
    settings = make(table_query="日本の人口")
    clip = SelectedClip(
        start=0, end=30, candidate_id="m", title="題", reason="r", confidence=1
    )
    visuals, ledger = pipeline.prepare_visuals(clip, settings, tmp_path, 30.0)
    assert [entry["kind"] for entry in ledger] == ["table"]
    assert ledger[0]["license"] == "CC BY-SA 4.0"
    assert (tmp_path / "visuals" / "table-01.png").is_file()
    overlay = visuals[0]
    assert overlay["position"] == "center"
    assert (overlay["start"], overlay["end"]) == (15, 23)


def test_no_table_query_means_no_table_search(tmp_path, monkeypatch):
    def must_not_search(query, limit=3):
        raise AssertionError("table search runs only when table_query is set")

    monkeypatch.setattr(pipeline.tables, "search", must_not_search)
    clip = SelectedClip(
        start=0, end=30, candidate_id="m", title="題", reason="r", confidence=1
    )
    assert pipeline.prepare_visuals(clip, make(), tmp_path, 30.0) == ([], [])


def test_diagram_values_and_rows_are_validated():
    with pytest.raises(ValidationError):
        Diagram(kind="bars", title="t", start=0, end=1, labels=["a"], values=[-1])
    with pytest.raises(ValidationError):
        Diagram(kind="table", title="t", start=0, end=1, rows=[[]])


def test_overlapping_speech_is_counted_once():
    assert signals._covered_seconds([(0, 10), (5, 15), (20, 22)]) == 17
    assert signals._covered_seconds([]) == 0


def test_a_silent_video_yields_non_overlapping_candidates_and_says_when_short():
    settings = make(candidates=8, min_seconds=20, max_seconds=75)
    raw = signals.candidates(
        {"duration": 300},
        Transcript(language="ja", segments=[]),
        {"boundaries": []},
        {"events": [], "status": "not_requested"},
        settings,
    )
    # Eight 75 s windows cannot all fit in 300 s without overlapping by more than 45%.
    assert 5 <= len(raw) < 8


def test_chat_panel_clip_writes_chat_beside_the_video_and_records_diagnostics(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    source = tmp_path / "in.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "out" / "clips"
    root.mkdir(parents=True)
    settings = make(aspect="16:9", height=1080, chat_panel=True)
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
    )
    chat = {
        "ass": "[Script Info]\nPlayResX: 1920\n",
        "messages": 0,
        "reason": "chat_unavailable",
        "error": "ChatError",
    }
    seen = {}

    def fake_render(
        source,
        destination,
        clip,
        settings,
        ass=None,
        keep=None,
        visuals=(),
        chat_ass=None,
        crop_x=None,
    ):
        seen["chat_ass"] = chat_ass
        seen["chat_text"] = chat_ass.read_text(encoding="utf-8")
        destination.write_bytes(b"video")
        return {
            "duration": 10,
            "width": 1920,
            "height": 1080,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    def fake_captions(
        transcript, clip, directory, settings, keep=None, credits=None, lifts=None
    ):
        pysubs2.SSAFile().save(str(directory / "captions.ass"))
        pysubs2.SSAFile().save(str(directory / "captions.srt"))
        return {"events": 0, "heading": False, "missing_glyphs": []}

    monkeypatch.setattr(pipeline.media, "render", fake_render)
    monkeypatch.setattr(pipeline, "create_captions", fake_captions)
    result = pipeline.render_clip(
        source, root, 1, clip, transcript, settings, "job-key", chat=chat
    )
    assert seen["chat_ass"].name == "chat.ass"
    assert seen["chat_text"] == chat["ass"]
    assert result["chat"] == {
        "messages": 0,
        "reason": "chat_unavailable",
        "error": "ChatError",
    }
    assert "chat.ass" in result["sha256"]
    assert result["quality"]["passed"] is True
    # The panel needs its chat source; a clip without one is refused before any work.
    with pytest.raises(PipelineError, match="Kick VOD URL"):
        pipeline.render_clip(source, root, 2, clip, transcript, settings, "job-key")


def _fill_clip_case():
    """Two captions, one at 0.5 s and one at 3.5 s, on a 6 s clip of a 16:9 1080 chat render."""
    transcript = Transcript(
        language="ja",
        segments=[
            Segment(
                start=s, end=s + 2, text=t, words=[Word(start=s, end=s + 2, text=t)]
            )
            for s, t in ((0.5, "上の字幕です"), (3.5, "下の字幕です"))
        ],
    )
    clip = SelectedClip(
        start=0, end=6, candidate_id="m", title="題", reason="r", confidence=1
    )
    return transcript, clip, make(aspect="16:9", height=1080, chat_panel=True)


def test_chat_layout_takes_the_planned_crop_and_caption_lifts(monkeypatch):
    transcript, clip, settings = _fill_clip_case()
    calls = []

    def plan(source, start, end, events, keep):
        calls.append((source, start, end, events, keep))
        return {
            "crop": {"anchor": "right", "x": 0.25, "kept": {}},
            "lifts": [0.2, 0.12],
        }

    monkeypatch.setattr(pipeline, "_auto_layout_plan", plan)
    layout = pipeline.chat_layout("in.mp4", clip, transcript, settings)
    assert layout == {
        "anchor": "right",
        "requested": "auto",
        "x": 0.25,
        "lifts": [0.2, 0.12],
        "static": None,
        "reason": None,
    }
    # Caption times are the file's own; the kept share is the area of the 1920 px frame.
    _, start, end, events, keep = calls[0]
    assert (start, end, events, keep) == (0, 6, [(0.5, 2.5), (3.5, 5.5)], 0.75)


def test_chat_layout_fixed_side_places_captions_without_choosing_the_crop(monkeypatch):
    transcript, clip, settings = _fill_clip_case()
    auto_calls, windows = [], []

    def auto(*args):
        auto_calls.append(args)
        return {"crop": {"anchor": "left", "x": 0.0, "kept": {}}, "lifts": []}

    def window(source, start, end, events, anchor, keep):
        windows.append(anchor)
        crop = {
            "anchor": anchor,
            "x": pipeline._crop_fractions(keep)[anchor],
            "keep": keep,
        }
        return {"crop": crop, "lifts": [0.12, 0.2]}

    monkeypatch.setattr(pipeline, "_auto_layout_plan", auto)
    monkeypatch.setattr(pipeline, "_auto_layout_window", window)
    for anchor, x in (("left", 0.0), ("center", 0.125), ("right", 0.25)):
        layout = pipeline.chat_layout("in.mp4", clip, transcript, settings, anchor)
        assert layout["anchor"] == anchor and layout["requested"] == anchor
        assert layout["x"] == pytest.approx(x) and layout["lifts"] == [0.12, 0.2]
    assert windows == ["left", "center", "right"] and auto_calls == []


def test_fixed_side_window_keeps_the_overlay_out_of_the_captions(monkeypatch):
    # Dark detail under the right 80 columns (the overlay) and a flat picture elsewhere.
    frames = np.full((4, 180, 320), 128, dtype=np.uint8)
    frames[:, :, 240:] = 0
    dark = np.random.default_rng(3).integers(
        0, 31, size=(180 - 140, 70), dtype=np.uint8
    )
    frames[:, 140:, 250:] = dark
    monkeypatch.setattr(
        auto_layout,
        "sample_frames",
        lambda *args, **kwargs: ([0.0, 0.5, 1.0, 1.5], frames),
    )
    window = pipeline._auto_layout_window("in.mp4", 0, 2, [(0.0, 0.5)], "right", 0.75)
    assert window["crop"]["overlay"]["side"] == "right"
    assert window["lifts"] == [pytest.approx(captions.FILL_BOTTOM_SHARE)]


def test_chat_layout_failure_falls_back_to_centre_or_the_fixed_side(monkeypatch):
    transcript, clip, settings = _fill_clip_case()

    def broken(*args):
        raise PipelineError("no video frames in the layout sample window")

    monkeypatch.setattr(pipeline, "_auto_layout_plan", broken)
    monkeypatch.setattr(pipeline, "_auto_layout_window", broken)
    assert pipeline.chat_layout("in.mp4", clip, transcript, settings) == {
        "anchor": "center",
        "requested": "auto",
        "x": None,
        "lifts": None,
        "static": None,
        "reason": "no video frames in the layout sample window",
    }
    fixed = pipeline.chat_layout("in.mp4", clip, transcript, settings, "right")
    assert fixed == {
        "anchor": "right",
        "requested": "right",
        "x": 0.25,
        "lifts": None,
        "static": None,
        "reason": "no video frames in the layout sample window",
    }


def test_chat_layout_refuses_a_lift_for_some_captions_only(monkeypatch):
    transcript, clip, settings = _fill_clip_case()
    monkeypatch.setattr(
        pipeline,
        "_auto_layout_plan",
        lambda *args, **kwargs: {
            "crop": {"anchor": "center", "x": 0.125, "kept": {}},
            "lifts": [0.2],
        },
    )
    layout = pipeline.chat_layout("in.mp4", clip, transcript, settings)
    assert layout["lifts"] is None
    assert layout["reason"] == "auto layout gave one lift per caption"


def test_layout_bugs_are_raised_not_hidden_as_the_base_margin(monkeypatch):
    transcript, clip, settings = _fill_clip_case()

    def bug(*args, **kwargs):
        raise TypeError("bug in the layout planner")

    monkeypatch.setattr(pipeline, "_auto_layout_plan", bug)
    with pytest.raises(TypeError, match="bug in the layout planner"):
        pipeline.chat_layout("in.mp4", clip, transcript, settings)


def test_standard_layout_raises_a_bug_instead_of_keeping_the_margin(monkeypatch):
    transcript, clip, settings = _standard_clip_case()
    monkeypatch.setattr(
        pipeline.media, "probe", lambda path: {"width": 1920, "height": 1080}
    )

    def bug(*args):
        raise TypeError("bug in the layout planner")

    monkeypatch.setattr(pipeline, "_auto_layout_window", bug)
    with pytest.raises(TypeError, match="bug in the layout planner"):
        pipeline.standard_layout("in.mp4", clip, transcript, settings, None)


def test_render_clip_records_the_chat_layout_and_keys_the_requested_side(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    source = tmp_path / "in.mp4"
    source.write_bytes(b"source")
    settings = make(aspect="16:9", height=1080, chat_panel=True)
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
    )
    chat = {
        "ass": "[Script Info]\nPlayResX: 1920\n",
        "messages": 0,
        "reason": None,
        "error": None,
    }
    count = len(captions.event_spans(transcript, clip, settings))
    lifts = [0.2] + [0.12] * (count - 1)
    seen = []

    def fake_render(
        source,
        destination,
        clip,
        settings,
        ass=None,
        keep=None,
        visuals=(),
        chat_ass=None,
        crop_x=None,
    ):
        seen.append(crop_x)
        destination.write_bytes(b"video")
        return {
            "duration": 10,
            "width": 1920,
            "height": 1080,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    def fake_captions(
        transcript, clip, directory, settings, keep=None, credits=None, lifts=None
    ):
        seen.append(lifts)
        pysubs2.SSAFile().save(str(directory / "captions.ass"))
        pysubs2.SSAFile().save(str(directory / "captions.srt"))
        return {"events": 0, "heading": False, "missing_glyphs": []}

    monkeypatch.setattr(pipeline.media, "render", fake_render)
    monkeypatch.setattr(pipeline, "create_captions", fake_captions)
    monkeypatch.setattr(
        pipeline,
        "_auto_layout_plan",
        lambda *args, **kwargs: {
            "crop": {"anchor": "right", "x": 0.25, "kept": {}},
            "lifts": lifts,
        },
    )
    monkeypatch.setattr(
        pipeline,
        "_auto_layout_window",
        lambda source, start, end, events, anchor, keep: {
            "crop": {
                "anchor": anchor,
                "x": pipeline._crop_fractions(keep)[anchor],
                "keep": keep,
            },
            "lifts": lifts,
        },
    )
    roots = {name: tmp_path / name / "clips" for name in ("auto", "left")}
    for root in roots.values():
        root.mkdir(parents=True)
    auto = pipeline.render_clip(
        source, roots["auto"], 1, clip, transcript, settings, "job", chat=chat
    )
    fixed = pipeline.render_clip(
        source,
        roots["left"],
        1,
        clip,
        transcript,
        settings,
        "job",
        chat=chat,
        crop_anchor="left",
    )
    assert auto["layout"] == {
        "anchor": "right",
        "requested": "auto",
        "x": 0.25,
        "lift_min": min(lifts),
        "lift_max": max(lifts),
        "lifted": sum(lift > 0.12 for lift in lifts),
        "reason": None,
        "floor": None,
        "static": None,
    }
    assert fixed["layout"] == {
        "anchor": "left",
        "requested": "left",
        "x": 0.0,
        "lift_min": min(lifts),
        "lift_max": max(lifts),
        "lifted": sum(lift > 0.12 for lift in lifts),
        "reason": None,
        "floor": None,
        "static": None,
    }
    assert seen == [lifts, 0.25, lifts, 0.0]
    # A different requested side is a different render, so its receipt has its own key.
    assert auto["key"] != fixed["key"]

    def no_analysis(*args, **kwargs):
        raise AssertionError("a verified cached render must not analyse frames again")

    monkeypatch.setattr(pipeline, "chat_layout", no_analysis)
    cached = pipeline.render_clip(
        source, roots["auto"], 1, clip, transcript, settings, "job", chat=chat
    )
    assert cached == auto
    assert seen == [lifts, 0.25, lifts, 0.0]
    receipt_file = roots["auto"] / "clip-001" / "render.json"
    stored = json.loads(receipt_file.read_text())
    stored["render_inputs"]["layout"]["requested"] = "left"
    receipt_file.write_text(json.dumps(stored))
    with pytest.raises(PipelineError, match="existing clip differs"):
        pipeline.render_clip(
            source, roots["auto"], 1, clip, transcript, settings, "job", chat=chat
        )
    with pytest.raises(PipelineError, match="crop_anchor must be"):
        pipeline.render_clip(
            source,
            roots["left"],
            2,
            clip,
            transcript,
            settings,
            "job",
            chat=chat,
            crop_anchor="middle",
        )


def _standard_clip_case():
    """The two-caption transcript of the chat cases, on a 16:9 1080 render without the chat column."""
    transcript, clip, _ = _fill_clip_case()
    return transcript, clip, make(aspect="16:9", height=1080)


def test_standard_layout_takes_the_crop_of_a_wide_source_and_its_lifts(monkeypatch):
    transcript, clip, _ = _standard_clip_case()
    settings = make(aspect="16:9", height=1080, framing="center-crop")
    windows = []
    monkeypatch.setattr(
        pipeline.media, "probe", lambda path: {"width": 2560, "height": 1080}
    )

    def window(source, start, end, events, anchor, keep):
        windows.append((anchor, keep))
        return {
            "crop": {"anchor": anchor, "x": 0.125, "keep": keep},
            "lifts": [0.2, 0.12],
            "bands": [[[0.0, 0.2, "whole"]], []],
        }

    monkeypatch.setattr(pipeline, "_auto_layout_window", window)
    layout = pipeline.standard_layout("in.mp4", clip, transcript, settings, None)
    # A 16:9 frame of a 2560 wide source is a centred crop keeping three quarters of its width.
    assert windows == [("center", 0.75)]
    assert layout == {
        "anchor": "center",
        "requested": "auto",
        "x": 0.125,
        "lifts": [0.2, 0.12],
        "static": [[[0.0, 0.2, "whole"]], []],
        "reason": None,
    }


def test_standard_layout_of_a_16_9_source_keeps_the_whole_picture(monkeypatch):
    transcript, clip, settings = _standard_clip_case()
    windows = []
    monkeypatch.setattr(
        pipeline.media, "probe", lambda path: {"width": 1920, "height": 1080}
    )
    monkeypatch.setattr(
        pipeline,
        "_auto_layout_window",
        lambda source, start, end, events, anchor, keep: windows.append(keep)
        or {"crop": {"x": 0.0}, "lifts": [0.12, 0.12], "bands": [[], []]},
    )
    layout = pipeline.standard_layout("in.mp4", clip, transcript, settings, None)
    assert windows == [1.0] and layout["anchor"] == "full"


def test_standard_layout_failure_and_unmodelled_cases_keep_the_base_margin(monkeypatch):
    transcript, clip, settings = _standard_clip_case()
    monkeypatch.setattr(
        pipeline.media, "probe", lambda path: {"width": 1920, "height": 1080}
    )

    def broken(*args):
        raise PipelineError("no video frames in the layout sample window")

    monkeypatch.setattr(pipeline, "_auto_layout_window", broken)
    assert pipeline.standard_layout("in.mp4", clip, transcript, settings, None) == {
        "anchor": "full",
        "requested": "auto",
        "x": None,
        "lifts": None,
        "static": None,
        "reason": "no video frames in the layout sample window",
    }
    panels = make(aspect="16:9", height=1080, layout="panels", face_box=(0, 0, 1, 1))
    assert (
        pipeline.standard_layout("in.mp4", clip, transcript, panels, None)["reason"]
        == "panels"
    )
    off = make(aspect="16:9", height=1080, caption_lift=False)
    assert (
        pipeline.standard_layout("in.mp4", clip, transcript, off, None)["reason"]
        == "disabled"
    )
    assert make().caption_lift is True


def test_caption_times_map_back_to_the_source_after_silence_cuts():
    clip = SelectedClip(
        start=5, end=20, candidate_id="m", title="題", reason="r", confidence=1
    )
    keep = [(10.0, 12.0), (20.0, 25.0)]
    assert pipeline._source_time(1.5, clip, keep) == pytest.approx(11.5)
    assert pipeline._source_time(2.5, clip, keep) == pytest.approx(20.5)
    assert pipeline._source_time(1.5, clip, None) == pytest.approx(6.5)


def test_landscape_layout_refusals_and_floor_without_lifts(monkeypatch):
    transcript, clip, settings = _standard_clip_case()
    with pytest.raises(PipelineError, match="landscape 16:9"):
        pipeline.landscape_layout("in.mp4", clip, transcript, make(), None, "auto", 0.2)
    top = make(aspect="16:9", height=1080, chat_panel=True, chat_panel_fit="top")
    with pytest.raises(PipelineError, match="chat fill"):
        pipeline.landscape_layout("in.mp4", clip, transcript, top, None, "auto", 0.2)
    assert (
        pipeline.landscape_layout("in.mp4", clip, transcript, top, None, "auto", None)
        is None
    )
    off = make(aspect="16:9", height=1080, caption_lift=False)
    assert (
        pipeline.landscape_layout("in.mp4", clip, transcript, off, None, "auto", None)
        is None
    )
    floored = pipeline.landscape_layout(
        "in.mp4", clip, transcript, off, None, "auto", 0.2
    )
    assert floored["lifts"] == [0.2, 0.2]
    assert floored["floor"] == 0.2 and floored["reason"] == "disabled"


def test_render_clip_applies_the_reviewed_caption_floor(
    tmp_path, monkeypatch, japanese_font, transcript, on_target_loudness
):
    monkeypatch.setattr(pipeline.fonts, "resolve", lambda family: japanese_font)
    source = tmp_path / "in.mp4"
    source.write_bytes(b"source")
    settings = make(aspect="16:9", height=1080)
    clip = SelectedClip(
        start=0, end=10, candidate_id="m", title="題", reason="r", confidence=1
    )
    count = len(captions.event_spans(transcript, clip, settings))
    seen = []

    def fake_render(
        source,
        destination,
        clip,
        settings,
        ass=None,
        keep=None,
        visuals=(),
        chat_ass=None,
        crop_x=None,
    ):
        seen.append(("crop_x", crop_x))
        destination.write_bytes(b"video")
        return {
            "duration": 10,
            "width": 1920,
            "height": 1080,
            "audio": True,
            "audio_sample_rate": 48000,
        }

    def fake_captions(
        transcript, clip, directory, settings, keep=None, credits=None, lifts=None
    ):
        seen.append(("lifts", lifts))
        pysubs2.SSAFile().save(str(directory / "captions.ass"))
        pysubs2.SSAFile().save(str(directory / "captions.srt"))
        return {"events": 0, "heading": False, "missing_glyphs": []}

    monkeypatch.setattr(pipeline.media, "render", fake_render)
    monkeypatch.setattr(pipeline, "create_captions", fake_captions)
    monkeypatch.setattr(
        pipeline.media, "probe", lambda path: {"width": 1920, "height": 1080}
    )
    monkeypatch.setattr(
        pipeline,
        "_auto_layout_window",
        lambda source, start, end, events, anchor, keep: {
            "crop": {"anchor": anchor, "x": 0.0, "keep": keep},
            "lifts": [0.12] * len(events),
            "bands": [[] for _ in events],
        },
    )
    with pytest.raises(PipelineError, match="caption_floor must be"):
        pipeline.render_clip(
            source,
            tmp_path / "bad",
            1,
            clip,
            transcript,
            settings,
            "job",
            caption_floor=0.4,
        )
    for name in ("bad", "plain", "floored"):
        (tmp_path / name).mkdir()
    plain = pipeline.render_clip(
        source, tmp_path / "plain", 1, clip, transcript, settings, "job"
    )
    floored = pipeline.render_clip(
        source,
        tmp_path / "floored",
        1,
        clip,
        transcript,
        settings,
        "job",
        caption_floor=0.25,
    )
    assert plain["layout"]["floor"] is None and plain["layout"]["lift_max"] == 0.12
    assert floored["layout"]["floor"] == 0.25
    assert floored["layout"]["lift_min"] == 0.25
    assert floored["layout"]["static"] == [[] for _ in range(count)]
    assert plain["key"] != floored["key"]
    assert ("lifts", [0.25] * count) in seen
    assert ("lifts", [0.12] * count) in seen
    # Render receipts are per clip directory; the crop is not a chat crop here.
    assert all(value is None for name, value in seen if name == "crop_x")
    # With caption_lift off and no floor, the render has no layout and its captions use the margin.
    seen.clear()
    (tmp_path / "off").mkdir()
    off = pipeline.render_clip(
        source,
        tmp_path / "off",
        1,
        clip,
        transcript,
        make(aspect="16:9", height=1080, caption_lift=False),
        "job",
    )
    assert "layout" not in off
    assert ("lifts", None) in seen

    def no_analysis(*args, **kwargs):
        raise AssertionError("cached standard layout must not decode frames again")

    monkeypatch.setattr(pipeline, "standard_layout", no_analysis)
    cached = pipeline.render_clip(
        source,
        tmp_path / "floored",
        1,
        clip,
        transcript,
        settings,
        "job",
        caption_floor=0.25,
    )
    assert cached == floored
    with pytest.raises(PipelineError, match="existing clip differs"):
        pipeline.render_clip(
            source,
            tmp_path / "floored",
            1,
            clip,
            transcript,
            settings,
            "job",
            caption_floor=0.2,
        )


def test_pause_marks_become_fullwidth_spaces_inside_and_vanish_at_the_end():
    tokens = [(0, 1, "よし、"), (1, 2, "平地に"), (2, 3, "しよう。")]
    assert captions.pause_marks(tokens) == [
        (0, 1, "よし　"),
        (1, 2, "平地に"),
        (2, 3, "しよう"),
    ]
    # A mark alone at the end empties its token but keeps its timing.
    assert captions.pause_marks([(0, 1, "はい、"), (1, 2, " そう"), (2, 3, "、")]) == [
        (0, 1, "はい　"),
        (1, 2, "そう"),
        (2, 3, ""),
    ]
    # Repeated marks give one space; a caption of marks only is left as it was.
    assert captions.pause_marks([(0, 1, "そう、、だね")]) == [(0, 1, "そう　だね")]
    assert captions.pause_marks([(0, 1, "、"), (1, 2, "。")]) == [
        (0, 1, "、"),
        (1, 2, "。"),
    ]
