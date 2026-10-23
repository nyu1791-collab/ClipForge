import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import audio_first as af, pipeline, signals
from clipforge.models import Transcript
from clipforge.storage import PipelineError, digest, write_json

URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"


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
                "rendition_height": 1080,
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

    def asr(*args, **kwargs):
        events.append(("asr",))
        return transcript.model_dump()

    monkeypatch.setattr(af, "audio_info", lambda p: {"audio": True, "duration": 300})
    monkeypatch.setattr(signals, "transcribe", asr)
    monkeypatch.setattr(
        af.topics, "split", lambda *a: [(0, 100), (100, 200), (200, 300)]
    )
    monkeypatch.setattr(
        signals, "scene_boundaries", lambda *a: pytest.fail("full video was scanned")
    )
    return VOD(), root, settings, events, transcript


def test_audio_then_topics_then_video_reuses_single_asr(audio_job):
    vod, root, settings, events, _ = audio_job
    af.acquire(vod, root)
    assert events == [("audio", 0, None)]
    assert not (root / "transcripts").exists()
    analysis, transcript = af.analyze(vod, root, settings)
    assert [e[0] for e in events] == ["audio", "asr"]
    assert len(analysis["topics"]) == 3 and transcript.segments
    assert analysis["semantic_topic_review"] == "required"
    af.previews(vod, root, settings)
    assert [e[0] for e in events] == ["audio", "asr", "preview", "preview", "preview"]
    assert json.loads((root / "asr-receipt.json").read_text())["reused"] is True
    af.previews(vod, root, settings)
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
    result = af.previews(
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
        af.previews(vod, root, settings, topic_ids=ids)
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
    preview = af.previews(vod, root, settings)
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
    af.previews(vod, root, settings)
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
    assert af.render(vod, root, settings, review)["status"] == "complete"
    assert [e[0] for e in events].count("asr") == 1
    assert [e for e in events if e[0] == "1080p"] == [
        ("1080p", 0, 85),
        ("1080p", 120, 175),
        ("1080p", 210, 295),
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
    af.render(vod, root, settings, review, only=1)
    af.render(
        vod,
        root,
        settings.model_copy(update={field: value}),
        review,
        only=1,
    )
    assert len(keys) == 2
    assert (keys[0] == keys[1]) is same_key
    assert [e for e in events if e[0] == "1080p"] == [("1080p", 0, 85)]


def test_changed_preview_blocks_all_high_quality_fetch(audio_job):
    vod, root, settings, events, _ = audio_job
    af.previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    review = root / "review.json"
    write_json(review, make_review(analysis))
    (root / "previews/topic-001.mp4").write_bytes(b"changed")
    with pytest.raises(PipelineError, match="preview changed"):
        af.render(vod, root, settings, review)
    assert "1080p" not in [e[0] for e in events]


@pytest.mark.parametrize("silence_cut", [False, True])
def test_render_aligns_segment_clip_word_clock_and_keep(
    audio_job, monkeypatch, silence_cut
):
    from clipforge import captions
    from clipforge.models import SelectedClip

    vod, root, settings, _, _ = audio_job
    review = reviewed_intervals(audio_job)
    settings.silence_cut = silence_cut
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
        keep = pipeline.cut_regions(source, shifted, local, settings)
        assert keep == ([(2.5, 10.0)] if silence_cut else None)
        tokens, _ = captions.collect_tokens(shifted, local, keep)
        assert tokens[0][0] == 0.0
        assert tokens[-1][1] == 7.5
        observed.append(source)
        return {"directory": "clips/clip-001"}

    monkeypatch.setattr(pipeline, "render_clip", render)
    analysis = json.loads((root / "topics.json").read_text())
    af.render(
        vod, root, settings, review, analysis_result=(analysis, transcript), only=1
    )
    assert len(observed) == 1
    assert observed[0].stem == af.fingerprint(
        {"start": clip.start, "end": clip.end, "media_format": af.RANGE_FORMAT}
    )


def test_render_still_rejects_below_720p(audio_job, monkeypatch):
    vod, root, settings, _, _ = audio_job
    review = reviewed_intervals(audio_job)
    retrieve = vod.retrieve

    def low_resolution(*args, **kwargs):
        receipt = retrieve(*args, **kwargs)
        receipt["rendition_height"] = 480
        return receipt

    monkeypatch.setattr(vod, "retrieve", low_resolution)
    monkeypatch.setattr(
        pipeline, "render_clip", lambda *a: pytest.fail("low resolution was rendered")
    )
    with pytest.raises(PipelineError, match="below 720p"):
        af.render(vod, root, settings, review, only=1)


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
    af.previews(vod, root, settings)
    analysis = json.loads((root / "topics.json").read_text())
    review = root / "review.json"
    write_json(review, make_review(analysis))
    # The synthetic word at 90..115 crosses the review boundary at 100 seconds.
    with pytest.raises(PipelineError, match="ASR word"):
        af.render(vod, root, settings, review)
    assert not any(event[0] == "1080p" for event in events)
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


@pytest.mark.parametrize("step", ["audio", "topics", "previews", "render"])
def test_stage_entry_point_does_not_skip_ahead(tmp_path, monkeypatch, capsys, step):
    closed = []

    def vod(*args, **kwargs):
        assert kwargs["max_bytes"] == (
            6 * 1024**3 if step in {"audio", "topics"} else 1024**3
        )
        return SimpleNamespace(close=lambda: closed.append(True))

    monkeypatch.setattr(af, "KickVOD", vod)
    calls = []

    def acquire(vod, root):
        calls.append("audio")
        (root / "audio.m4a").write_bytes(b"synthetic audio")

    monkeypatch.setattr(af, "acquire", acquire)
    monkeypatch.setattr(
        af,
        "analyze",
        lambda *a: (calls.append("topics") or {"status": "topics_allocated"}, None),
    )
    monkeypatch.setattr(
        af,
        "previews",
        lambda *a: calls.append("previews") or {"status": "awaiting_video_review"},
    )
    monkeypatch.setattr(
        af, "render", lambda *a: calls.append("render") or {"status": "complete"}
    )
    args = ["--url", URL, "--workspace", str(tmp_path), "--stage", step]
    if step == "render":
        args += ["--review-file", "private-review.json"]
    assert af.main(args) == 0
    assert calls == [step] and closed == [True]
    assert json.loads(capsys.readouterr().out)["model_generation_requests"] == 0


INTERVALS = [(0, 85), (120, 175), (210, 295)]


def reviewed_intervals(audio_job):
    vod, root, settings, _, _ = audio_job
    af.previews(vod, root, settings)
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
    af.render(vod, root, settings, review)
    assert [e[1:] for e in events if e[0] == "prefetch"] == INTERVALS
    assert [e[1:] for e in events if e[0] == "1080p"] == INTERVALS
    for start, end in INTERVALS:
        # Each interval is warmed before its own fetch, and fetched exactly once.
        assert events.index(("prefetch", start, end)) < events.index(
            ("1080p", start, end)
        )
    for previous, (start, end) in zip(INTERVALS, INTERVALS[1:]):
        # The next prefetch is submitted only after the previous clip's fetch.
        assert events.index(("1080p", *previous)) < events.index(
            ("prefetch", start, end)
        )


def test_prefetch_failure_fails_that_clip_before_its_own_fetch(audio_job, monkeypatch):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    vod.prefetch_failure = 120
    stub_clip_render(monkeypatch, events)
    with pytest.raises(PipelineError, match="synthetic prefetch failure"):
        af.render(vod, root, settings, review)
    assert ("1080p", 0, 85) in events
    assert ("1080p", 120, 175) not in events
    assert ("1080p", 210, 295) not in events
    assert [e for e in events if e[0] == "render"] == [("render", 1)]


def test_render_records_seconds_per_clip_in_manifest_and_output(
    audio_job, monkeypatch, capsys
):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    stub_clip_render(monkeypatch, events)
    af.render(vod, root, settings, review)
    timings = json.loads((root / "delivery/manifest.json").read_text())["timings"]
    assert set(timings) == {"clip-001", "clip-002", "clip-003", "total"}
    for name in ("clip-001", "clip-002", "clip-003"):
        assert set(timings[name]) == {"prefetch_wait", "fetch", "render"}
        assert all(isinstance(v, float) and v >= 0 for v in timings[name].values())
    stage_seconds = [
        v for name in timings if name != "total" for v in timings[name].values()
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
        af.render(vod, root, settings, review, only=only)
    assert "1080p" not in [e[0] for e in events]


def test_only_fetches_and_renders_one_clip_without_lookahead(audio_job, monkeypatch):
    vod, root, settings, events, _ = audio_job
    review = reviewed_intervals(audio_job)
    stub_clip_render(monkeypatch, events)
    monkeypatch.setattr(
        af.landscape,
        "summarize_delivery",
        lambda *a: pytest.fail("a single clip must not summarize the delivery"),
    )
    manifest = af.render(vod, root, settings, review, only=2)
    assert [e for e in events if e[0] == "1080p"] == [("1080p", 120, 175)]
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
    assert set(manifest["timings"]) == {"clip-002", "total"}
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
    af.previews(vod, root, settings)
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
    af.render(vod, root, chat_settings(settings), review, only=2)
    # The chat is read on the reviewed interval in original seconds, not the shifted clock.
    assert fetched == [("example", URL.rsplit("/", 1)[1], 120, 175)]
    assert drawn[0]["messages"][0]["t"] == 121.5
    assert (drawn[0]["start"], drawn[0]["end"]) == (120, 175)
    assert (drawn[0]["width"], drawn[0]["height"], drawn[0]["panel_x"]) == (
        1920,
        1080,
        1440,
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
    af.render(vod, root, chat_settings(settings), review, only=2)
    assert drawn[0]["messages"] == []
    assert rendered[0] == {
        "ass": "[Script Info]\n",
        "messages": 0,
        "reason": "chat_unavailable",
        "error": "chat service refused",
    }


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
    af.render(vod, root, chat_settings(settings), review, only=2)
    assert anchors == [(2, "right")]


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
        af.render(vod, root, chat_settings(settings), review)
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
    af.render(vod, root, settings, review, only=2)
    assert floors == [(2, 0.25)]
