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

    def asr(*args):
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


@pytest.mark.parametrize(
    "field",
    ["source_url", "source_start", "source_end", "audio_only", "sha256", "missing"],
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
    write_json(review, make_review(analysis))

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
        ("1080p", 0, 100),
        ("1080p", 100, 200),
        ("1080p", 200, 300),
    ]
    manifest = json.loads((root / "delivery/manifest.json").read_text())
    assert manifest["rendered"][1]["source_interval"] == {
        "start": 100,
        "end": 200,
        "topic_id": "topic-002",
    }


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
    transcript.segments[0].words = []
    with pytest.raises(PipelineError, match="wordless"):
        af.shifted_transcript(transcript, 12, 14)
    assert af.shifted_transcript(transcript, 0, 5).segments == []
    assert af.shifted_transcript(transcript, 10, 14).segments[0].text == "前半後半"


def test_lock_prevents_duplicate_analysis(tmp_path):
    with af.exclusive(tmp_path):
        with pytest.raises(PipelineError, match="another"):
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
        af, "process", lambda *a: SimpleNamespace(stdout=json.dumps(data))
    )
    assert af.audio_info(tmp_path / "audio.m4a")["duration"] == 300
    data["format"]["duration"] = "nan"
    with pytest.raises(PipelineError):
        af.audio_info(tmp_path / "audio.m4a")


@pytest.mark.parametrize("step", ["audio", "topics", "previews", "render"])
def test_stage_entry_point_does_not_skip_ahead(tmp_path, monkeypatch, capsys, step):
    closed = []
    monkeypatch.setattr(
        af,
        "KickVOD",
        lambda *a, **k: SimpleNamespace(close=lambda: closed.append(True)),
    )
    calls = []
    monkeypatch.setattr(af, "acquire", lambda *a: calls.append("audio"))
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
