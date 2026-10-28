import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import audio_first as af, pipeline
from clipforge.models import Settings, Transcript
from clipforge.storage import PipelineError, digest, write_json

URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
KEY = "e" * 64


def analyze(vod, root, settings):
    """The analysis as the ASR job leaves it: audio, the saved transcript, then topics."""
    af.acquire(vod, root)
    saved = root / "transcripts" / (KEY + ".json")
    if not saved.exists():
        vod.events.append(("asr",))
        write_json(saved, vod.transcript.model_dump())
    pinned = settings.model_copy(
        update={"require_transcript_cache": True, "transcript_key": KEY}
    )
    return af.allocate_audio(root / "audio.m4a", root, pinned, URL)


def run_previews(vod, root, settings, analysis_result=None, **options):
    analysis_result = analysis_result or analyze(vod, root, settings)
    return af.previews(vod, root, settings, analysis_result, **options)


def run_render(vod, root, settings, review, analysis_result=None, **options):
    analysis_result = analysis_result or analyze(vod, root, settings)
    return af.render(vod, root, settings, review, analysis_result, **options)


@pytest.fixture
def audio_job(tmp_path, monkeypatch):
    # Every transport/ASR/render operation is synthetic; order is part of the contract.
    from clipforge.models import Settings

    settings = Settings.model_validate_json(
        Path("config.landscape.example.json").read_text()
    )
    settings.candidates = 3
    root = tmp_path / "private"
    root.mkdir()
    events = []

    class VOD:
        url = URL
        prefetch_failure = None

        def prefetch(self, start, end):
            events.append(("prefetch", start, end))
            if self.prefetch_failure is not None and start == self.prefetch_failure:
                raise PipelineError("synthetic prefetch failure")

        def retrieve(self, path, start=0, end=None, quality=None, audio_only=False):
            events.append(("audio" if audio_only else quality, start, end))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"synthetic {start} {end} {quality}".encode())
            return {
                "source_url": URL,
                "source_start": start,
                "source_end": end or 300,
                "source_duration": 300,
                "audio_only": audio_only,
                "rendition_height": 360,
                "sha256": digest(path),
                **(
                    {
                        "media_format": af.RANGE_FORMAT,
                        "start_offset": 0.0,
                        "covered_duration": (end or 300) - start,
                    }
                    if not audio_only and quality != "preview"
                    else {}
                ),
            }

    transcript = Transcript.model_validate(
        {
            "language": "ja",
            "segments": [
                {
                    "start": n,
                    "end": n + 25,
                    "text": f"合成の話題{n}",
                    "words": [{"start": n, "end": n + 25, "text": f"合成の話題{n}"}],
                }
                for n in range(0, 300, 30)
            ],
        }
    )

    VOD.events, VOD.transcript = events, transcript
    monkeypatch.setattr(af, "audio_info", lambda p: {"audio": True, "duration": 300})
    monkeypatch.setattr(
        af.topics, "split", lambda *a: [(0, 100), (100, 200), (200, 300)]
    )
    return VOD(), root, settings, events, transcript


def test_audio_then_topics_then_video_reuses_single_asr(audio_job):
    vod, root, settings, events, _ = audio_job
    af.acquire(vod, root)
    assert events == [("audio", 0, None)]
    assert not (root / "transcripts").exists()
    analysis, transcript = analyze(vod, root, settings)
    assert [e[0] for e in events] == ["audio", "asr"]
    assert len(analysis["topics"]) == 3 and transcript.segments
    assert analysis["semantic_topic_review"] == "required"
    run_previews(vod, root, settings)
    assert [e[0] for e in events] == ["audio", "asr", "preview", "preview", "preview"]
    assert json.loads((root / "asr-receipt.json").read_text())["reused"] is True
    run_previews(vod, root, settings)
    assert len(events) == 5


def test_saved_audio_size_is_checked_before_asr(audio_job):
    vod, root, settings, events, _ = audio_job
    af.acquire(vod, root)
    (root / "audio.m4a").write_bytes(b"x" * 1025)
    settings.max_input_bytes = 1024
    with pytest.raises(PipelineError, match="input size limit"):
        af.allocate_audio(root / "audio.m4a", root, settings, URL)
    assert events == [("audio", 0, None)]


def test_audio_review_can_choose_topics_before_video_fetch(audio_job):
    vod, root, settings, events, _ = audio_job
    result = run_previews(
        vod, root, settings, topic_ids=["topic-003", "topic-001", "topic-002"]
    )
    assert result["preview_selection_basis"] == "reviewed_audio_topics"
    assert [row["topic_id"] for row in result["previews"]] == [
        "topic-003",
        "topic-001",
        "topic-002",
    ]
    assert [row[1] for row in events if row[0] == "preview"] == [200, 0, 100]


@pytest.mark.parametrize(
    "ids", [["missing", "topic-002", "topic-003"], ["topic-001"] * 3]
)
def test_invalid_audio_topic_shortlist_stops_before_video_fetch(audio_job, ids):
    vod, root, settings, events, _ = audio_job
    with pytest.raises(PipelineError, match="shortlist"):
        run_previews(vod, root, settings, topic_ids=ids)
    assert not any(row[0] == "preview" for row in events)


@pytest.mark.parametrize(
    "field",
    [
        "source_url",
        "source_start",
        "source_end",
        "audio_only",
        "sha256",
        "media_format",
        "missing",
    ],
)
def test_cached_media_requires_matching_source_and_interval(audio_job, field):
    vod, root, _, events, _ = audio_job
    path = root / "range.mp4"
    af.retrieved(vod, path, 20, 80)
    receipt = path.with_suffix(".mp4.receipt.json")
    value = json.loads(receipt.read_text())
    if field == "missing":
        receipt.unlink()
    else:
        value[field] = "wrong"
        write_json(receipt, value)
    with pytest.raises(PipelineError):
        af.retrieved(vod, path, 20, 80)
    assert len(events) == 1


def test_cached_transcoded_range_without_format_is_rejected(audio_job):
    vod, root, _, events, _ = audio_job
    path = root / "range.mp4"
    af.retrieved(vod, path, 20, 80)
    receipt_path = path.with_suffix(".mp4.receipt.json")
    receipt = json.loads(receipt_path.read_text())
    del receipt["media_format"]
    write_json(receipt_path, receipt)
    with pytest.raises(PipelineError, match="cache source or interval mismatch"):
        af.retrieved(vod, path, 20, 80)
    assert len(events) == 1


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("start_offset", None),
        ("start_offset", -1),
        ("start_offset", float("nan")),
        ("covered_duration", None),
        ("covered_duration", 59),
    ],
)
def test_cached_remux_requires_valid_coverage(audio_job, field, invalid):
    vod, root, _, events, _ = audio_job
    path = root / "range.mp4"
    af.retrieved(vod, path, 20, 80)
    receipt_path = path.with_suffix(".mp4.receipt.json")
    receipt = json.loads(receipt_path.read_text())
    receipt[field] = invalid
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(PipelineError, match="cache coverage"):
        af.retrieved(vod, path, 20, 80)
    assert len(events) == 1


def make_review(analysis):
    return {
        "analysis_key": analysis["analysis_key"],
        "clips": [
            {
                "candidate_id": row["id"],
                "start": row["start"],
                "end": row["end"],
                "title": f"話題{n}",
                "reason": "synthetic review",
                "confidence": 1,
                "video_reviewed": True,
            }
            for n, row in enumerate(analysis["topics"])
        ],
    }


@pytest.mark.parametrize(
    "fault", ["analysis", "unreviewed", "not_previewed", "outside", "duplicate"]
)
def test_review_is_tied_to_audio_topics_and_previews(audio_job, fault):
    vod, root, settings, _, _ = audio_job
    preview = run_previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    value = make_review(analysis)
    if fault == "analysis":
        value["analysis_key"] = "wrong"
    elif fault == "unreviewed":
        value["clips"][0]["video_reviewed"] = False
    elif fault == "not_previewed":
        preview["previews"].pop()
    elif fault == "outside":
        value["clips"][0]["end"] = 110
        value["clips"][1]["start"] = 110
    else:
        value["clips"][1]["candidate_id"] = value["clips"][0]["candidate_id"]
    with pytest.raises(PipelineError):
        af.reviewed_clips(value, analysis, preview, settings)


def test_render_fetches_only_reviewed_intervals_without_second_asr(
    audio_job, monkeypatch
):
    vod, root, settings, events, _ = audio_job
    run_previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    review = root / "review.json"
    value = make_review(analysis)
    for clip, (start, end) in zip(value["clips"], [(0, 85), (120, 175), (210, 295)]):
        clip.update(start=start, end=end)
    write_json(review, value)

    def render(source, output, index, clip, transcript, settings, key):
        events.append(("render", clip.start, clip.end))
        assert clip.start == 0 and all(s.end <= 100 for s in transcript.segments)
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    monkeypatch.setattr(
        af.landscape, "summarize_delivery", lambda *a: {"status": "complete"}
    )
    assert run_render(vod, root, settings, review)["status"] == "complete"
    assert [e[0] for e in events].count("asr") == 1
    assert [e for e in events if e[0] == "360p"] == [
        ("360p", 0, 85),
        ("360p", 120, 175),
        ("360p", 210, 295),
    ]
    manifest = json.loads((root / "delivery/manifest.json").read_text())
    assert manifest["rendered"][1]["source_interval"] == {
        "start": 120,
        "end": 175,
        "topic_id": "topic-002",
    }


@pytest.mark.parametrize(
    ("field", "value", "same_key"),
    [("encode_threads", 4, True), ("height", 720, False)],
)
def test_render_key_ignores_only_encode_thread_changes(
    audio_job, monkeypatch, field, value, same_key
):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    keys = []

    def render(source, output, index, clip, transcript, settings, key):
        keys.append(key)
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    run_render(vod, root, settings, review, only=1)
    run_render(
        vod,
        root,
        settings.model_copy(update={field: value}),
        review,
        only=1,
    )
    assert len(keys) == 2
    assert (keys[0] == keys[1]) is same_key
    assert [e for e in events if e[0] == "360p"] == [("360p", 0, 85)]


def test_changed_preview_blocks_all_high_quality_fetch(audio_job):
    vod, root, settings, events, _ = audio_job
    run_previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    review = root / "review.json"
    write_json(review, make_review(analysis))
    (root / "previews/topic-001.mp4").write_bytes(b"changed")
    with pytest.raises(PipelineError, match="preview changed"):
        run_render(vod, root, settings, review)
    assert "360p" not in [e[0] for e in events]


def test_render_aligns_segment_clip_and_word_clock(audio_job, monkeypatch):
    from clipforge import captions
    from clipforge.models import SelectedClip

    vod, root, settings, _, _ = audio_job
    review = reviewed_intervals(audio_job)
    clip = SelectedClip(
        start=12.5,
        end=20,
        candidate_id="topic-001",
        title="synthetic",
        reason="r",
        confidence=1,
    )
    transcript = Transcript.model_validate(
        {
            "language": "ja",
            "segments": [
                {
                    "start": 12.5,
                    "end": 20,
                    "text": "合成の字幕",
                    "words": [{"start": 12.5, "end": 20, "text": "合成の字幕"}],
                }
            ],
        }
    )
    monkeypatch.setattr(af, "reviewed_clips", lambda *a: [clip])
    retrieve = vod.retrieve

    def remux(*args, **kwargs):
        receipt = retrieve(*args, **kwargs)
        receipt.update(start_offset=2.5, covered_duration=10.0)
        return receipt

    monkeypatch.setattr(vod, "retrieve", remux)
    observed = []

    def render(source, output, index, local, shifted, settings, key):
        assert local.start == 2.5 and local.end == 10.0
        segment = shifted.segments[0]
        assert (segment.start, segment.end) == (2.5, 10.0)
        assert (segment.words[0].start, segment.words[0].end) == (2.5, 10.0)
        tokens, _ = captions.collect_tokens(shifted, local)
        assert tokens[0][0] == 0.0
        assert tokens[-1][1] == 7.5
        observed.append(source)
        return {"directory": "clips/clip-001"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    analysis = json.loads((root / "topics.json").read_text())
    run_render(
        vod, root, settings, review, analysis_result=(analysis, transcript), only=1
    )
    assert len(observed) == 1
    assert observed[0].stem == af.fingerprint(
        {
            "start": clip.start,
            "end": clip.end,
            "media_format": af.RANGE_FORMAT,
            "quality": "360p",
        }
    )


def test_render_accepts_high_resolution_source(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = reviewed_intervals(audio_job)
    retrieve = vod.retrieve

    def high_resolution(*args, **kwargs):
        receipt = retrieve(*args, **kwargs)
        receipt["rendition_height"] = 480
        return receipt

    monkeypatch.setattr(vod, "retrieve", high_resolution)
    rendered = []
    def render(*args):
        rendered.append(True)
        return {"directory": "clips/clip-001"}
    monkeypatch.setattr(pipeline, "render_clip", render)
    run_render(vod, root, settings, review, only=1)
    assert rendered == [True]


def test_word_clock_and_text_shift_without_inventing_boundary_text():
    transcript = Transcript.model_validate(
        {
            "language": "ja",
            "segments": [
                {
                    "start": 10,
                    "end": 14,
                    "text": "前半後半",
                    "words": [
                        {"start": 10, "end": 12, "text": "前半"},
                        {"start": 12, "end": 14, "text": "後半"},
                    ],
                }
            ],
        }
    )
    shifted = af.shifted_transcript(transcript, 12, 14)
    assert shifted.segments[0].text == "後半"
    assert shifted.segments[0].start == 0 and shifted.segments[0].end == 2
    with pytest.raises(PipelineError, match="ASR word"):
        af.shifted_transcript(transcript, 12.5, 14)
    transcript.segments[0].words = []
    with pytest.raises(PipelineError, match="wordless"):
        af.shifted_transcript(transcript, 12, 14)
    assert af.shifted_transcript(transcript, 0, 5).segments == []
    assert af.shifted_transcript(transcript, 10, 14).segments[0].text == "前半後半"


def test_mid_word_review_is_rejected_before_any_high_quality_fetch(audio_job):
    vod, root, settings, events, _ = audio_job
    run_previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    review = root / "review.json"
    write_json(review, make_review(analysis))
    # The synthetic word at 90..115 crosses the review boundary at 100 seconds.
    with pytest.raises(PipelineError, match="ASR word"):
        run_render(vod, root, settings, review)
    assert not any(event[0] == "360p" for event in events)
    assert not (root / "delivery").exists()


def test_lock_prevents_duplicate_analysis(tmp_path):
    with af.exclusive(tmp_path), pytest.raises(PipelineError, match="another"):
        with af.exclusive(tmp_path):
            pytest.fail("duplicate lock")


def test_only_generated_transport_cache_is_discarded(tmp_path):
    transport = tmp_path / (("a" * 64) + ".ts")
    transport.write_bytes(b"temporary muxed HLS")
    transport.with_suffix(".json").write_text("{}")
    unrelated = tmp_path / "user-file.ts"
    unrelated.write_bytes(b"keep")
    af.discard_transport_cache(SimpleNamespace(cache=tmp_path))
    assert not transport.exists() and not transport.with_suffix(".json").exists()
    assert unrelated.read_bytes() == b"keep"


def test_audio_metadata_requires_finite_duration(tmp_path, monkeypatch):
    data = {
        "streams": [{"codec_type": "audio", "start_time": "0"}],
        "format": {"duration": "300"},
    }
    monkeypatch.setattr(
        af, "process", lambda *a, **kwargs: SimpleNamespace(stdout=json.dumps(data))
    )
    assert af.audio_info(tmp_path / "audio.m4a")["duration"] == 300
    data["format"]["duration"] = "nan"
    with pytest.raises(PipelineError):
        af.audio_info(tmp_path / "audio.m4a")


INTERVALS = [(0, 85), (120, 175), (210, 295)]


def reviewed_intervals(audio_job):
    vod, root, settings, _, _ = audio_job
    run_previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    value = make_review(analysis)
    for clip, (start, end) in zip(value["clips"], INTERVALS):
        clip.update(start=start, end=end)
    review = root / "review.json"
    write_json(review, value)
    return review


def stub_clip_render(monkeypatch, events):
    def render(source, output, index, clip, transcript, settings, key):
        events.append(("render", index))
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    monkeypatch.setattr(
        af.landscape, "summarize_delivery", lambda *a: {"status": "complete"}
    )


def test_next_interval_is_prefetched_once_while_the_current_clip_renders(
    audio_job, monkeypatch
):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    stub_clip_render(monkeypatch, events)
    run_render(vod, root, settings, review)
    assert [e[1:] for e in events if e[0] == "prefetch"] == INTERVALS
    assert [e[1:] for e in events if e[0] == "360p"] == INTERVALS
    for start, end in INTERVALS:
        # Each interval is warmed before its own fetch, and fetched exactly once.
        assert events.index(("prefetch", start, end)) < events.index(
            ("360p", start, end)
        )
    for previous, (start, end) in zip(INTERVALS, INTERVALS[1:]):
        # The next prefetch is submitted only after the previous clip's fetch.
        assert events.index(("360p", *previous)) < events.index(
            ("prefetch", start, end)
        )


def test_prefetch_failure_fails_that_clip_before_its_own_fetch(audio_job, monkeypatch):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    vod.prefetch_failure = 120
    stub_clip_render(monkeypatch, events)
    with pytest.raises(PipelineError, match="synthetic prefetch failure"):
        run_render(vod, root, settings, review)
    assert ("360p", 0, 85) in events
    assert ("360p", 120, 175) not in events
    assert ("360p", 210, 295) not in events
    assert [e for e in events if e[0] == "render"] == [("render", 1)]


def test_render_records_seconds_per_clip_in_manifest_and_output(
    audio_job, monkeypatch, capsys
):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    stub_clip_render(monkeypatch, events)
    run_render(vod, root, settings, review)
    timings = json.loads((root / "delivery/manifest.json").read_text())["timings"]
    assert set(timings) == {
        "clip-001",
        "clip-002",
        "clip-003",
        "total",
        "wall_seconds",
        "wall_clock_sec",
        "substep_breakdown",
    }
    for name in ("clip-001", "clip-002", "clip-003"):
        assert set(timings[name]) == {"prefetch_wait", "fetch", "render"}
        assert all(isinstance(v, float) and v >= 0 for v in timings[name].values())
    stage_seconds = [
        v
        for name in timings
        if name.startswith("clip-")
        for v in timings[name].values()
    ]
    assert timings["total"] == pytest.approx(sum(stage_seconds), abs=0.05)
    out = capsys.readouterr().out
    for index in (1, 2, 3):
        assert f"Clip {index}/3: fetch " in out
        assert (
            f"Clip {index}/3: fetch {timings[f'clip-{index:03}']['fetch']:.1f} s, render "
            in out
        )
    assert URL not in out


@pytest.mark.parametrize("only", [0, 4, True, "2"])
def test_only_must_name_one_reviewed_clip_before_any_fetch(
    audio_job, monkeypatch, only
):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    stub_clip_render(monkeypatch, events)
    with pytest.raises(PipelineError, match="only must name"):
        run_render(vod, root, settings, review, only=only)
    assert "360p" not in [e[0] for e in events]


def test_only_fetches_and_renders_one_clip_without_lookahead(audio_job, monkeypatch):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    stub_clip_render(monkeypatch, events)
    monkeypatch.setattr(
        af.landscape,
        "summarize_delivery",
        lambda *a: pytest.fail("a single clip must not summarize the delivery"),
    )
    manifest = run_render(vod, root, settings, review, only=2)
    assert [e for e in events if e[0] == "360p"] == [("360p", 120, 175)]
    assert [e[1:] for e in events if e[0] == "prefetch"] == [(120, 175)]
    assert [e for e in events if e[0] == "render"] == [("render", 2)]
    analysis = json.loads((root / "topics.json").read_text())
    selection = root / "review.json"
    assert manifest == json.loads((root / "delivery/manifest.json").read_text())
    assert manifest["status"] == "clip_rendered"
    assert manifest["clip_index"] == 2 and manifest["clip_count"] == 3
    assert manifest["analysis_key"] == analysis["analysis_key"]
    assert manifest["selection_sha256"] == digest(selection)
    assert manifest["rendered"] == [
        {
            "directory": "clips/clip-002",
            "source_interval": {"start": 120, "end": 175, "topic_id": "topic-002"},
        }
    ]
    assert set(manifest["timings"]) == {
        "clip-002",
        "total",
        "wall_seconds",
        "wall_clock_sec",
        "substep_breakdown",
    }
    assert set(manifest["timings"]["clip-002"]) == {"prefetch_wait", "fetch", "render"}
    assert not (root / "delivery/e2e-summary.json").exists()


def chat_settings(settings):
    from clipforge.models import Settings

    return Settings.model_validate({**settings.model_dump(), "chat_panel": True})


def fake_kick_chat(monkeypatch, fetch):
    """Replaces the kick_chat calls; records what the panel was drawn from."""
    drawn = []

    def panel_ass(messages, clip_start, clip_end, **options):
        drawn.append(
            {"messages": messages, "start": clip_start, "end": clip_end, **options}
        )
        return "[Script Info]\n"

    from clipforge import kick_chat

    monkeypatch.setattr(kick_chat, "fetch_messages", fetch)
    monkeypatch.setattr(kick_chat, "panel_ass", panel_ass)
    return drawn


def chat_review(audio_job):
    vod, root, settings, _, _ = audio_job
    run_previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    value = make_review(analysis)
    for clip, (start, end) in zip(value["clips"], [(0, 85), (120, 175), (210, 295)]):
        clip.update(start=start, end=end)
    review = root / "review.json"
    write_json(review, value)
    return review


def test_chat_panel_clip_is_drawn_from_its_original_interval(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = chat_review(audio_job)
    fetched = []

    def fetch(slug, uuid, start, end, **options):
        fetched.append((slug, uuid, start, end))
        return [{"t": start + 1.5, "name": "viewer", "color": "#ffffff", "text": "hi"}]

    drawn = fake_kick_chat(monkeypatch, fetch)
    rendered = []

    def render(
        source,
        output,
        index,
        clip,
        transcript,
        settings,
        key,
        chat=None,
        crop_anchor="auto",
    ):
        rendered.append(chat)
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    run_render(vod, root, chat_settings(settings), review, only=2)
    # The chat is read on the reviewed interval in original seconds, not the shifted clock.
    assert fetched == [("example", URL.rsplit("/", 1)[1], 120, 175)]
    assert drawn[0]["messages"][0]["t"] == 121.5
    assert (drawn[0]["start"], drawn[0]["end"]) == (120, 175)
    assert (drawn[0]["width"], drawn[0]["height"], drawn[0]["panel_x"]) == (
        640,
        360,
        480,
    )
    assert drawn[0]["anonymize"] is False
    assert rendered[0] == {
        "ass": "[Script Info]\n",
        "messages": 1,
        "reason": None,
        "error": None,
    }


def test_chat_fetch_failure_keeps_the_clip_with_an_empty_column(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = chat_review(audio_job)

    def fetch(*args, **options):
        raise PipelineError("chat service refused")

    drawn = fake_kick_chat(monkeypatch, fetch)
    rendered = []

    def render(
        source,
        output,
        index,
        clip,
        transcript,
        settings,
        key,
        chat=None,
        crop_anchor="auto",
    ):
        rendered.append(chat)
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    run_render(vod, root, chat_settings(settings), review, only=2)
    assert drawn[0]["messages"] == []
    assert rendered[0] == {
        "ass": "[Script Info]\n",
        "messages": 0,
        "reason": "chat_unavailable",
        "error": "chat service refused",
    }


def test_chat_panel_on_a_non_kick_source_is_drawn_empty(monkeypatch):
    calls = []
    drawn = fake_kick_chat(monkeypatch, lambda *args, **kwargs: calls.append(args))
    config = Path(__file__).parents[1] / "config.landscape-chat.example.json"
    settings = Settings.model_validate_json(config.read_text())
    vod = SimpleNamespace(url="https://example.com/archive/stream.m3u8")
    result = af.chat_panel_for_clip(
        vod, SimpleNamespace(start=10.0, end=40.0), settings
    )
    assert calls == []
    assert drawn[0]["messages"] == []
    assert result["reason"] == "chat_unsupported_source"


def test_crop_anchors_default_to_auto_and_refuse_other_words():
    assert af.crop_anchors({"clips": [{}, {"crop_anchor": "left"}]}) == ["auto", "left"]
    with pytest.raises(PipelineError, match="crop_anchor must be"):
        af.crop_anchors({"clips": [{"crop_anchor": "centre"}]})


def test_chat_failure_is_recorded_and_a_bug_is_not_hidden(audio_job, monkeypatch):
    from types import SimpleNamespace

    vod, _, settings, _, _ = audio_job
    clip = SimpleNamespace(start=120, end=175)

    def refused(slug, uuid, start, end, **options):
        raise PipelineError("chat service refused")

    fake_kick_chat(monkeypatch, refused)
    chat = af.chat_panel_for_clip(vod, clip, chat_settings(settings))
    assert (chat["messages"], chat["reason"], chat["error"]) == (
        0,
        "chat_unavailable",
        "chat service refused",
    )

    def broken(slug, uuid, start, end, **options):
        raise TypeError("bug in the chat reader")

    fake_kick_chat(monkeypatch, broken)
    with pytest.raises(TypeError, match="bug in the chat reader"):
        af.chat_panel_for_clip(vod, clip, chat_settings(settings))


def test_chat_panel_clip_passes_its_reviewed_crop_side(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = chat_review(audio_job)
    value = json.loads(review.read_text())
    value["clips"][1]["crop_anchor"] = "right"
    write_json(review, value)
    fake_kick_chat(monkeypatch, lambda slug, uuid, start, end, **options: [])
    anchors = []

    def render(
        source,
        output,
        index,
        clip,
        transcript,
        settings,
        key,
        chat=None,
        crop_anchor="auto",
    ):
        anchors.append((index, crop_anchor))
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    run_render(vod, root, chat_settings(settings), review, only=2)
    assert anchors == [(2, "right")]


def test_a_rendered_clip_is_not_read_for_chat_again(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = chat_review(audio_job)
    fetched = []

    def fetch(slug, uuid, start, end, **options):
        fetched.append((start, end))
        return []

    fake_kick_chat(monkeypatch, fetch)
    folder = root / "delivery" / "clips" / "clip-002"
    folder.mkdir(parents=True)
    write_json(
        folder / "render.json", {"render_inputs": {"chat": None, "layout": None}}
    )
    rendered = []

    def render(
        source,
        output,
        index,
        clip,
        transcript,
        settings,
        key,
        chat=None,
        crop_anchor="auto",
    ):
        rendered.append((index, chat, crop_anchor))
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    run_render(vod, root, chat_settings(settings), review, only=2)
    assert fetched == []
    assert rendered == [(2, None, "auto")]


def test_render_refuses_an_unknown_crop_side_before_any_fetch(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = chat_review(audio_job)
    value = json.loads(review.read_text())
    value["clips"][0]["crop_anchor"] = "middle"
    write_json(review, value)
    fetched = []
    monkeypatch.setattr(
        vod, "prefetch", lambda *args: fetched.append(args), raising=False
    )
    with pytest.raises(PipelineError, match="crop_anchor must be"):
        run_render(vod, root, chat_settings(settings), review)
    assert fetched == []


def test_caption_floors_default_to_none_and_refuse_out_of_range():
    assert af.caption_floors({"clips": [{}, {"caption_floor": 0.25}]}) == [None, 0.25]
    for floor in (0.33, -0.1, True, "0.2", float("nan")):
        with pytest.raises(PipelineError, match="caption_floor must be"):
            af.caption_floors({"clips": [{"caption_floor": floor}]})


def test_render_passes_a_reviewed_caption_floor_to_its_clip(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = reviewed_intervals(audio_job)
    value = json.loads(review.read_text())
    value["clips"][1]["caption_floor"] = 0.25
    review.write_text(json.dumps(value))
    floors = []

    def render(source, output, index, clip, transcript, settings, key, **options):
        floors.append((index, options.get("caption_floor")))
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    monkeypatch.setattr(
        af.landscape, "summarize_delivery", lambda *a: {"status": "complete"}
    )
    run_render(vod, root, settings, review, only=2)
    assert floors == [(2, 0.25)]


def test_previews_follow_the_hint_ranking_before_speech_length(tmp_path, monkeypatch):
    from clipforge import audio_first as first
    from clipforge.models import Settings

    settings = Settings(clips=3, candidates=3)
    topics = [
        {"id": "t-long", "start": 0, "end": 60, "speech_seconds": 50, "text": "あ"},
        {"id": "t-short", "start": 100, "end": 160, "speech_seconds": 5, "text": "い"},
        {"id": "t-mid", "start": 200, "end": 260, "speech_seconds": 20, "text": "う"},
    ]
    seen = []

    def fake_retrieved(vod, target, start, end, quality):
        seen.append(target.stem)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"preview")
        return {"sha256": "0" * 64}

    monkeypatch.setattr(first, "retrieved", fake_retrieved)
    analysis = {"analysis_key": "k", "topics": topics}
    first.previews(
        None,
        tmp_path,
        settings,
        analysis_result=(analysis, None),
        ranking=["t-short", "t-mid", "t-long"],
    )
    assert seen == ["t-short", "t-mid", "t-long"]


def caption_transcript(*lines):
    rows = []
    for start, text in lines:
        words = [
            {"text": c, "start": start + 0.1 * i, "end": start + 0.1 * (i + 1)}
            for i, c in enumerate(text)
        ]
        rows.append(
            {"start": start, "end": words[-1]["end"], "text": text, "words": words}
        )
    return Transcript.model_validate({"language": "ja", "segments": rows})


def test_a_caption_edit_changes_only_the_nearest_matching_line():
    transcript = caption_transcript((100.0, "雪ナイトだよ"), (130.0, "雪ナイトです"))
    edited = af.edited_transcript(
        transcript, 100.0, [{"at": 29.0, "from": "雪ナイト", "to": "ユキナイト"}]
    )
    assert [s.text for s in edited.segments] == ["雪ナイトだよ", "ユキナイトです"]
    words = edited.segments[1].words
    assert [w.text for w in words][:5] == list("ユキナイト")
    assert words[0].start == pytest.approx(130.0)
    assert words[4].end == pytest.approx(130.4)


@pytest.mark.parametrize(
    "edit",
    [
        {"at": 0.0, "from": "ない語", "to": "x"},
        {"at": 60.0, "from": "雪ナイト", "to": "x"},
    ],
)
def test_a_caption_edit_without_a_matching_line_or_word_is_refused(edit):
    with pytest.raises(PipelineError):
        af.edited_transcript(caption_transcript((100.0, "雪ナイトだよ")), 100.0, [edit])


@pytest.mark.parametrize(
    "edits",
    [
        "x",
        [{"at": "1", "from": "a", "to": "b"}],
        [{"at": 1, "from": "", "to": "b"}],
        [{"at": 1, "from": "a"}],
        [{"at": -1, "from": "a", "to": "b"}],
    ],
)
def test_malformed_caption_edits_are_refused(edits):
    with pytest.raises(PipelineError, match="caption_edits"):
        af.caption_edits({"clips": [{"caption_edits": edits}]})


def test_reviewed_caption_edits_reach_only_their_clip(audio_job, monkeypatch):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    value = json.loads(review.read_text())
    value["clips"][1]["caption_edits"] = [
        {"at": 0.0, "from": "合成の話題120", "to": "直した話題"}
    ]
    write_json(review, value)
    texts = {}

    def render(source, output, index, clip, transcript, settings, key):
        texts[index] = "".join(s.text for s in transcript.segments)
        return {"directory": f"clips/clip-{index:03}"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    monkeypatch.setattr(
        af.landscape, "summarize_delivery", lambda *a: {"status": "complete"}
    )
    run_render(vod, root, settings, review)
    assert texts[2].startswith("直した話題")
    assert "直した話題" not in texts[1] + texts[3]


def test_an_unmatched_caption_edit_stops_before_any_high_quality_fetch(audio_job):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    value = json.loads(review.read_text())
    value["clips"][0]["caption_edits"] = [{"at": 0.0, "from": "無い語", "to": "x"}]
    write_json(review, value)
    before = len(events)
    with pytest.raises(PipelineError, match="no caption"):
        run_render(vod, root, settings, review)
    assert all(event[0] in {"audio", "asr"} for event in events[before:])
