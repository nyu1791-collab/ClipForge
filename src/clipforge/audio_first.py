"""Audio-first Kick analysis, topic previews and reviewed landscape rendering.

Each stage is explicit: acquiring audio never starts ASR or downloads high-quality video.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys

from . import landscape, pipeline, topics
from .kick_media import KickVOD, canonical
from .models import Transcript
from .storage import PipelineError, digest, fingerprint, process, stage, write_json


@contextmanager
def exclusive(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".audio-first.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineError("another audio-first job is active") from None
        yield


def audio_info(path):
    data = json.loads(
        process(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                path,
            ]
        ).stdout
    )
    stream = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    duration = float(data["format"]["duration"])
    if stream is None or not math.isfinite(duration) or duration <= 0:
        raise PipelineError("valid audio is required")
    return {
        "audio": True,
        "duration": duration,
        "audio_start": float(stream.get("start_time") or 0),
    }


def retrieved(vod, path, start=0, end=None, *, audio_only=False, quality="1080p"):
    """A media hash alone cannot establish which original interval it contains."""
    receipt_path = path.with_suffix(path.suffix + ".receipt.json")
    expected = {"source_url": vod.url, "source_start": start, "audio_only": audio_only}
    if end is not None:
        expected["source_end"] = end
    if path.exists() or receipt_path.exists():
        if (
            path.is_symlink()
            or receipt_path.is_symlink()
            or not path.is_file()
            or not receipt_path.is_file()
        ):
            raise PipelineError("incomplete media cache")
        receipt = json.loads(receipt_path.read_text())
        if (
            any(receipt.get(k) != v for k, v in expected.items())
            or receipt.get("sha256") != digest(path)
            or (
                end is None
                and receipt.get("source_end") != receipt.get("source_duration")
            )
        ):
            raise PipelineError("media cache source or interval mismatch")
        return receipt
    receipt = vod.retrieve(
        path, start=start, end=end, audio_only=audio_only, quality=quality
    )
    write_json(receipt_path, receipt)
    return receipt


def acquire(vod, root):
    receipt = retrieved(vod, root / "audio.m4a", audio_only=True, quality="lowest")
    write_json(
        root / "acquisition.json",
        {
            "status": "audio_acquired",
            "source_url": vod.url,
            "audio": receipt,
            "asr_started": False,
            "high_quality_video_fetched": False,
            "transport": "audio rendition if advertised, otherwise lowest HLS rendition; saved audio only",
        },
    )
    discard_transport_cache(vod)
    return receipt


def discard_transport_cache(vod):
    """After audio is committed, discard the temporary muxed low-resolution transport."""
    cache = getattr(vod, "cache", None)
    if cache is not None:
        for path in Path(cache).glob("*.ts"):
            if re.fullmatch(r"[a-f0-9]{64}\.ts", path.name):
                path.unlink()
                path.with_suffix(".json").unlink(missing_ok=True)


def analyze(vod, root, settings):
    acquire(vod, root)
    source = root / "audio.m4a"
    return allocate_audio(source, root, settings, vod.url)


def allocate_audio(source, root, settings, source_url):
    """Worker entry point for an already acquired audio file; no Kick or video fetch."""
    canonical(source_url)
    info = audio_info(source)
    if info["duration"] > settings.max_source_seconds:
        raise PipelineError("source duration exceeds the analysis budget")
    settings = settings.model_copy(
        update={"transcript_cache": str(root / "transcripts")}
    )
    receipt = {}
    transcript = Transcript.model_validate(
        pipeline.transcribe_once(source, info, settings, receipt)
    )
    key = fingerprint(
        {
            "transcript": transcript.model_dump(),
            "asr": receipt["key"],
            "min": settings.topic_min_seconds,
            "max": settings.max_seconds,
            "topics_code": digest(Path(topics.__file__)),
        }
    )

    def allocate():
        rows = []
        for start, end in topics.split(
            transcript,
            info["duration"],
            settings.topic_min_seconds,
            settings.max_seconds,
        ):
            segments = [
                s for s in transcript.segments if s.end > start and s.start < end
            ]
            if not segments or end - start < settings.min_seconds:
                continue
            rows.append(
                {
                    "id": f"topic-{len(rows) + 1:03}",
                    "start": start,
                    "end": end,
                    "speech_seconds": round(
                        sum(
                            max(0, min(s.end, end) - max(s.start, start))
                            for s in segments
                        ),
                        3,
                    ),
                    "text": "".join(s.text for s in segments),
                }
            )
        return {
            "status": "topics_allocated",
            "analysis_key": key,
            "source_url": source_url,
            "duration": info["duration"],
            "topics": rows,
            "selection_method": "local_lexical_cohesion_and_pauses",
            "semantic_topic_review": "required",
            "video_review_performed": False,
            "model_generation_requests": 0,
        }

    result = stage(root, "topics", key, allocate)
    write_json(root / "asr-receipt.json", receipt)
    return result, transcript


def previews(vod, root, settings):
    analysis, _ = analyze(vod, root, settings)
    ordered = sorted(
        analysis["topics"], key=lambda row: (-row["speech_seconds"], row["start"])
    )
    rows = []
    for topic in ordered[: settings.candidates]:
        target = root / "previews" / (topic["id"] + ".mp4")
        receipt = retrieved(
            vod, target, topic["start"], topic["end"], quality="preview"
        )
        rows.append(
            {
                "topic_id": topic["id"],
                "file": str(target.relative_to(root)),
                "receipt": receipt,
            }
        )
    value = {
        "status": "awaiting_video_review",
        "analysis_key": analysis["analysis_key"],
        "previews": rows,
        "video_review_performed": False,
        "model_generation_requests": 0,
    }
    write_json(root / "previews.json", value)
    return value


def reviewed_clips(value, analysis, preview, settings):
    if (
        value.get("analysis_key") != analysis["analysis_key"]
        or preview.get("analysis_key") != analysis["analysis_key"]
    ):
        raise PipelineError("video review belongs to a different audio analysis")
    if not all(row.get("video_reviewed") is True for row in value["clips"]):
        raise PipelineError("each selected topic requires video review")
    cleaned = {
        "clips": [
            {k: v for k, v in row.items() if k != "video_reviewed"}
            for row in value["clips"]
        ]
    }
    clips = landscape.validate_selection(cleaned, settings, analysis["duration"])
    available = {row["id"]: row for row in analysis["topics"]}
    seen_previews = {row["topic_id"] for row in preview["previews"]}
    for clip in clips:
        topic = available.get(clip.candidate_id)
        if (
            topic is None
            or clip.candidate_id not in seen_previews
            or not topic["start"] <= clip.start < clip.end <= topic["end"]
        ):
            raise PipelineError(
                "selected interval must belong to a previewed audio topic"
            )
    if len({clip.candidate_id for clip in clips}) != len(clips):
        raise PipelineError("select distinct audio topics")
    return clips


def shifted_transcript(transcript, start, end):
    """Keep full-source ASR; clip and shift its word clock without recognising again."""
    segments = []
    for segment in transcript.segments:
        if segment.start >= end or segment.end <= start:
            continue
        words = [
            dict(
                start=max(w.start, start) - start,
                end=min(w.end, end) - start,
                text=w.text,
            )
            for w in segment.words
            if w.end > start and w.start < end
        ]
        if not segment.words and (segment.start < start or segment.end > end):
            raise PipelineError("wordless speech cannot be trimmed inside a segment")
        text = "".join(w["text"] for w in words) if segment.words else segment.text
        segments.append(
            dict(
                start=max(segment.start, start) - start,
                end=min(segment.end, end) - start,
                text=text,
                words=words,
            )
        )
    return Transcript(language=transcript.language, segments=segments)


def render(vod, root, settings, review_file):
    analysis, transcript = analyze(vod, root, settings)
    preview = json.loads((root / "previews.json").read_text())
    value = json.loads(review_file.read_text())
    clips = reviewed_clips(value, analysis, preview, settings)
    # Verify all previews before fetching even the first high-quality interval.
    for row in preview["previews"]:
        topic = next(t for t in analysis["topics"] if t["id"] == row["topic_id"])
        target = root / "previews" / (topic["id"] + ".mp4")
        if not target.is_file() or row["receipt"].get("sha256") != digest(target):
            raise PipelineError("reviewed preview changed")
    output = root / "delivery"
    output.mkdir(exist_ok=True)
    (output / "clips").mkdir(exist_ok=True)
    write_json(
        output / "manifest.json",
        {
            "status": "rendering",
            "rendered": [],
            "analysis_key": analysis["analysis_key"],
            "selection_sha256": digest(review_file),
        },
    )
    key = fingerprint(
        {
            "review": value,
            "analysis": analysis["analysis_key"],
            "settings": settings.model_dump(),
            "code": landscape.code_identity(settings)["implementation_sha256"],
        }
    )
    rows = []
    for index, clip in enumerate(clips, 1):
        target = (
            root
            / "ranges"
            / f"{fingerprint({'start': clip.start, 'end': clip.end})}.mp4"
        )
        receipt = retrieved(vod, target, clip.start, clip.end, quality="1080p")
        if receipt["rendition_height"] < 720:
            raise PipelineError("selected source video is below 720p")
        local = clip.model_copy(update={"start": 0.0, "end": clip.end - clip.start})
        row = pipeline.render_clip(
            target,
            output / "clips",
            index,
            local,
            shifted_transcript(transcript, clip.start, clip.end),
            settings,
            fingerprint({"job": key, "source": receipt["sha256"]}),
        )
        row["source_interval"] = {
            "start": clip.start,
            "end": clip.end,
            "topic_id": clip.candidate_id,
        }
        rows.append(row)
    manifest = {
        "status": "complete",
        "rendered": rows,
        "selection_receipt": {"provider": "manual", "generation_requests": 0},
        "analysis_key": analysis["analysis_key"],
        "selection_sha256": digest(review_file),
        "video_review_attested": True,
        "human_review_performed": False,
        "transcript_cache": json.loads((root / "asr-receipt.json").read_text()),
    }
    summary = landscape.summarize_delivery(manifest, output, settings.clips)
    summary["topic_content_review"] = "attested_in_selection_file"
    summary["reproducibility"] = landscape.code_identity(settings)
    write_json(output / "manifest.json", manifest)
    write_json(output / "e2e-summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--stage", choices=("audio", "topics", "previews", "render"), default="audio"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.landscape.example.json")
    )
    parser.add_argument("--review-file", type=Path)
    args = parser.parse_args(argv)
    vod = None
    try:
        os.umask(0o077)
        from .models import Settings

        settings = Settings.model_validate_json(args.config.read_text())
        if (settings.aspect, settings.height, settings.quality_gate) != (
            "16:9",
            1080,
            "strict",
        ):
            raise PipelineError("audio-first delivery is landscape with a strict gate")
        if settings.image_search or settings.table_query or settings.chat_url:
            raise PipelineError("external lookups must be disabled")
        canonical(args.url)
        root = args.workspace.resolve()
        with exclusive(root):
            identity = root / "source.json"
            if identity.exists() and json.loads(identity.read_text()) != {
                "url": args.url
            }:
                raise PipelineError("workspace belongs to another source")
            write_json(identity, {"url": args.url})
            vod = KickVOD(
                args.url,
                root / "segments",
                max_bytes=settings.max_input_bytes,
                max_seconds=settings.max_source_seconds,
            )
            if args.stage == "audio":
                acquire(vod, root)
                status = "audio_acquired"
            elif args.stage == "topics":
                result, _ = analyze(vod, root, settings)
                status = result["status"]
            elif args.stage == "previews":
                status = previews(vod, root, settings)["status"]
            else:
                if args.review_file is None:
                    raise PipelineError("render requires reviewed intervals")
                status = render(vod, root, settings, args.review_file)["status"]
        print(
            json.dumps(
                {"status": status, "stage": args.stage, "model_generation_requests": 0}
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, PipelineError):
        print(
            "Audio-first stage failed; inspect private local records. No model fallback was started.",
            file=sys.stderr,
        )
        return 1
    finally:
        if vod is not None:
            vod.close()


if __name__ == "__main__":
    raise SystemExit(main())
