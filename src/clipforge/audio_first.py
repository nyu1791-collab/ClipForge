"""Audio-first Kick analysis, topic previews and reviewed landscape rendering.

Each stage is explicit: acquiring audio never starts ASR or downloads high-quality video.
"""

import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import zlib

from . import landscape, media, pipeline, topics
from .kick_media import RANGE_FORMAT, KickVOD
from .media_sources import canonical, open_source
from pydantic import ValidationError

from .caption_pack import pack_captions  # noqa: F401  (re-exported for callers)
from .models import Transcript
from .storage import (
    TIME_TOLERANCE,
    PipelineError,
    digest,
    fingerprint,
    process,
    stage,
    write_json,
)


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
            ],
            stage="audio-info",
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
    if not audio_only and quality != "preview":
        expected["media_format"] = RANGE_FORMAT
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
        if "media_format" in expected:
            offset, covered = (
                receipt.get("start_offset"),
                receipt.get("covered_duration"),
            )
            if (
                any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    for v in (offset, covered)
                )
                or not 0 <= offset < covered
                or offset + (receipt["source_end"] - start) > covered + TIME_TOLERANCE
            ):
                raise PipelineError("invalid media cache coverage")
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
            "high_quality_video_fetched": receipt.get(
                "high_quality_video_fetched", False
            ),
            "transport": receipt.get(
                "transport",
                "audio rendition if advertised, otherwise lowest HLS rendition; saved audio only",
            ),
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
    if not 0 < source.stat().st_size <= settings.max_input_bytes:
        raise PipelineError("audio exceeds the input size limit")
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


def previews(vod, root, settings, analysis_result=None, topic_ids=None, ranking=None):
    """Preview the best candidates first.

    ``ranking`` lists topic IDs from the text, speech and loudness hints, best first. Without
    it, speech length alone decides, which can drop a strong short candidate.
    """
    analysis, _ = (
        analysis_result if analysis_result is not None else analyze(vod, root, settings)
    )
    position = {topic_id: index for index, topic_id in enumerate(ranking or [])}
    ordered = sorted(
        analysis["topics"],
        key=lambda row: (
            position.get(row["id"], len(position)),
            -row["speech_seconds"],
            row["start"],
        ),
    )
    if topic_ids is not None:
        available = {row["id"]: row for row in analysis["topics"]}
        if (
            not isinstance(topic_ids, list)
            or not settings.clips <= len(topic_ids) <= settings.candidates
            or any(
                not isinstance(topic_id, str) or topic_id not in available
                for topic_id in topic_ids
            )
            or len(set(topic_ids)) != len(topic_ids)
        ):
            raise PipelineError(
                "audio topic shortlist must contain distinct valid topic IDs"
            )
        ordered = [available[topic_id] for topic_id in topic_ids]
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
        "preview_selection_basis": "reviewed_audio_topics"
        if topic_ids is not None
        else "speech_duration",
    }
    write_json(root / "previews.json", value)
    return value


def crop_anchors(value):
    """The crop side each reviewed clip requests; "auto" unless its row names one."""
    anchors = []
    for row in value["clips"]:
        anchor = row.get("crop_anchor", "auto")
        if anchor not in pipeline.CROP_ANCHORS:
            raise PipelineError("crop_anchor must be auto, left, center or right")
        anchors.append(anchor)
    return anchors


def caption_floors(value):
    """The caption floor each reviewed clip requests (0 to 0.32 of the height); None if unnamed."""
    floors = []
    for row in value["clips"]:
        floor = row.get("caption_floor")
        if floor is None:
            floors.append(None)
            continue
        if (
            type(floor) not in (int, float)
            or not math.isfinite(floor)
            or not 0 <= floor <= pipeline.MAX_CAPTION_FLOOR
        ):
            raise PipelineError("caption_floor must be from 0 to 0.32")
        floors.append(float(floor))
    return floors


# A reviewed clip may carry captions recognised separately for its interval, packed as
# base64 zlib JSON: [[start_cs, end_cs, text, [[word, start_cs, end_cs], ...]], ...] in
# source centiseconds. They replace the full-source ASR for that clip only.
MAX_CAPTION_BYTES = 2 * 1024 * 1024


def caption_overrides(value, language):
    """The Transcript each reviewed clip supplies for its own captions, or None."""
    overrides = []
    for row in value["clips"]:
        packed = row.get("captions")
        if packed is None:
            overrides.append(None)
            continue
        try:
            if not isinstance(packed, str):
                raise TypeError
            inflate = zlib.decompressobj()
            raw = inflate.decompress(
                base64.b64decode(packed, validate=True), MAX_CAPTION_BYTES + 1
            )
            if len(raw) > MAX_CAPTION_BYTES or inflate.unconsumed_tail:
                raise PipelineError("reviewed captions are oversized")
            rows = json.loads(raw)
            overrides.append(
                Transcript.model_validate(
                    {
                        "language": language,
                        "segments": [
                            {
                                "start": start / 100,
                                "end": end / 100,
                                "text": text,
                                "words": [
                                    {"text": w, "start": a / 100, "end": b / 100}
                                    for w, a, b in words
                                ],
                            }
                            for start, end, text, words in rows
                        ],
                    }
                )
            )
        except PipelineError:
            raise
        except (
            TypeError,
            ValueError,
            binascii.Error,
            zlib.error,
            ValidationError,
        ):
            raise PipelineError(
                "reviewed captions must be packed caption segments"
            ) from None
    return overrides


def reviewed_clips(value, analysis, preview, settings):
    if (
        value.get("analysis_key") != analysis["analysis_key"]
        or preview.get("analysis_key") != analysis["analysis_key"]
    ):
        raise PipelineError("video review belongs to a different audio analysis")
    if not all(row.get("video_reviewed") is True for row in value["clips"]):
        raise PipelineError("each selected topic requires video review")
    if any("caption_edits" in row for row in value["clips"]):
        # Edits are applied where the captions run's captions are attached.
        raise PipelineError("caption edits need captions attached from a captions run")
    crop_anchors(value)
    caption_floors(value)
    cleaned = {
        "clips": [
            {
                k: v
                for k, v in row.items()
                if k
                not in ("video_reviewed", "crop_anchor", "caption_floor", "captions")
            }
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


def shifted_transcript(transcript, start, end, offset=0.0):
    """Keep full-source ASR; clip and shift its word clock without recognising again."""
    segments = []
    for segment in transcript.segments:
        if segment.start >= end or segment.end <= start:
            continue
        if any(w.start < start < w.end or w.start < end < w.end for w in segment.words):
            raise PipelineError(
                "selected interval cuts through an ASR word; adjust review boundaries"
            )
        words = [
            dict(
                start=max(w.start, start) - start + offset,
                end=min(w.end, end) - start + offset,
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
                start=max(segment.start, start) - start + offset,
                end=min(segment.end, end) - start + offset,
                text=text,
                words=words,
            )
        )
    return Transcript(language=transcript.language, segments=segments)


def prefetch_interval(vod, clip, target):
    """Warm the cache for one reviewed interval; no ffmpeg and no output is written."""
    if not target.exists():
        vod.prefetch(clip.start, clip.end)


def chat_panel_for_clip(vod, clip, settings):
    """Chat ASS for one reviewed clip, timed on the original video.

    A failed chat fetch does not fail the clip: the column is drawn empty and the reason is
    returned for the clip's diagnostics.
    """
    # Imported here: only chat-panel renders need the chat module.
    from . import kick_chat

    kick = re.fullmatch(r"https://kick\.com/([A-Za-z0-9_-]+)/videos/([^/]+)", vod.url)
    messages, reason, error = [], None, None
    if kick is None:
        # Chat replay is read from Kick only; other sources get an empty column.
        reason = "chat_unsupported_source"
    else:
        try:
            messages = kick_chat.fetch_messages(*kick.groups(), clip.start, clip.end)
        except PipelineError as failure:
            reason, error = "chat_unavailable", str(failure)[:200]
    width, height = media.frame_size(settings)
    ass = kick_chat.panel_ass(
        messages,
        clip.start,
        clip.end,
        width=width,
        height=height,
        panel_x=width - settings.chat_panel_pixels(),
        font=settings.font,
        anonymize=settings.chat_anonymize,
    )
    return {
        "ass": ass,
        "messages": len(messages),
        "reason": reason,
        "error": error,
    }


def render(vod, root, settings, review_file, analysis_result=None, only=None):
    analysis, transcript = (
        analysis_result if analysis_result is not None else analyze(vod, root, settings)
    )
    preview = json.loads((root / "previews.json").read_text())
    value = json.loads(review_file.read_text())
    clips = reviewed_clips(value, analysis, preview, settings)
    anchors = crop_anchors(value)
    floors = caption_floors(value)
    if only is not None and (type(only) is not int or not 1 <= only <= len(clips)):
        raise PipelineError("only must name one reviewed clip by its 1-based number")
    # Verify all previews before fetching even the first high-quality interval.
    for row in preview["previews"]:
        topic = next(t for t in analysis["topics"] if t["id"] == row["topic_id"])
        target = root / "previews" / (topic["id"] + ".mp4")
        if not target.is_file() or row["receipt"].get("sha256") != digest(target):
            raise PipelineError("reviewed preview changed")
    overrides = caption_overrides(value, transcript.language)
    local_transcripts = [
        shifted_transcript(override or transcript, clip.start, clip.end)
        for clip, override in zip(clips, overrides)
    ]
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
            "settings": settings.model_dump(exclude={"encode_threads"}),
            "code": landscape.code_identity(settings)["implementation_sha256"],
            "media_format": RANGE_FORMAT,
        }
    )
    targets = [
        root
        / "ranges"
        / f"{fingerprint({'start': clip.start, 'end': clip.end, 'media_format': RANGE_FORMAT})}.mp4"
        for clip in clips
    ]
    rows, timings = [], {}
    indexes = range(1, len(clips) + 1) if only is None else [only]
    # The next clip's segments are fetched over HTTP while the current clip is rendered.
    # A single-clip render has no next clip, so it warms only its own interval.
    with ThreadPoolExecutor(max_workers=1) as ahead:
        pending = ahead.submit(
            prefetch_interval,
            vod,
            clips[indexes[0] - 1],
            targets[indexes[0] - 1],
        )
        for index in indexes:
            clip = clips[index - 1]
            local_transcript = local_transcripts[index - 1]
            target = targets[index - 1]
            clip_timings = {}
            # Re-raises a prefetch failure as this clip's failure, before its fetch.
            pipeline.timed(clip_timings, "prefetch_wait", pending.result)
            receipt = pipeline.timed(
                clip_timings,
                "fetch",
                lambda: retrieved(vod, target, clip.start, clip.end, quality="1080p"),
            )
            if index < indexes[-1]:
                pending = ahead.submit(
                    prefetch_interval, vod, clips[index], targets[index]
                )
            if receipt["rendition_height"] < 720:
                raise PipelineError("selected source video is below 720p")
            start_offset = receipt["start_offset"]
            local = clip.model_copy(
                update={
                    "start": start_offset,
                    "end": start_offset + (clip.end - clip.start),
                }
            )
            local_transcript = shifted_transcript(
                local_transcript, 0.0, clip.end - clip.start, offset=start_offset
            )
            # Chat is timed on the clip's original interval, not the shifted local one.
            chat_options = {}
            if settings.chat_panel:
                chat_options["crop_anchor"] = anchors[index - 1]
                # A clip already rendered with its inputs saved reuses its stored chat, so the chat
                # is not read again.
                if pipeline.saved_render_inputs(output / "clips", index) is None:
                    chat_options["chat"] = pipeline.timed(
                        clip_timings, "chat", chat_panel_for_clip, vod, clip, settings
                    )
            if floors[index - 1] is not None:
                chat_options["caption_floor"] = floors[index - 1]
            row = pipeline.timed(
                clip_timings,
                "render",
                lambda: pipeline.render_clip(
                    target,
                    output / "clips",
                    index,
                    local,
                    local_transcript,
                    settings,
                    fingerprint({"job": key, "source": receipt["sha256"]}),
                    **chat_options,
                ),
            )
            row["source_interval"] = {
                "start": clip.start,
                "end": clip.end,
                "topic_id": clip.candidate_id,
            }
            rows.append(row)
            timings[f"clip-{index:03}"] = clip_timings
            print(
                f"Clip {index}/{len(clips)}: fetch {clip_timings['fetch']:.1f} s, "
                f"render {clip_timings['render']:.1f} s",
                flush=True,
            )
    # Total wall-clock seconds spent waiting, fetching and rendering, summed over clips.
    timings["total"] = round(
        sum(v for t in timings.values() if isinstance(t, dict) for v in t.values()), 2
    )
    if only is not None:
        # One clip's part; the whole-delivery summary waits for the merge stage.
        manifest = {
            "status": "clip_rendered",
            "clip_index": only,
            "clip_count": len(clips),
            "rendered": rows,
            "analysis_key": analysis["analysis_key"],
            "selection_sha256": digest(review_file),
            "timings": timings,
        }
        write_json(output / "manifest.json", manifest)
        return manifest
    manifest = {
        "status": "complete",
        "rendered": rows,
        "selection_receipt": {"provider": "manual", "generation_requests": 0},
        "analysis_key": analysis["analysis_key"],
        "selection_sha256": digest(review_file),
        "video_review_attested": True,
        "human_review_performed": False,
        "transcript_cache": json.loads((root / "asr-receipt.json").read_text()),
        "timings": timings,
    }
    summary = landscape.summarize_delivery(
        manifest, output, settings.clips, settings.height
    )
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
    parser.add_argument("--cookies-file", type=Path)
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
            vod = open_source(
                args.url,
                root / "segments",
                kick_factory=KickVOD,
                **({"cookies_file": args.cookies_file} if args.cookies_file else {}),
                max_bytes=(
                    settings.max_transport_bytes
                    if args.stage in {"audio", "topics"}
                    else settings.max_input_bytes
                ),
                max_seconds=settings.max_source_seconds,
            )
            if args.stage == "audio":
                acquire(vod, root)
                if (
                    not 0
                    < (root / "audio.m4a").stat().st_size
                    <= settings.max_input_bytes
                ):
                    raise PipelineError("audio exceeds the input size limit")
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
