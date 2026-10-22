"""Two independently recognised audio parts, joined on the original audio clock."""

import hashlib
import math
import re
import wave

from . import media, pipeline
from .models import Segment, Transcript, Word
from .storage import (
    TIME_TOLERANCE,
    PipelineError,
    digest,
    fingerprint,
    process,
    write_json,
)

OVERLAP_SECONDS = 5


def part_id(parent, index):
    if (
        not re.fullmatch(r"[a-f0-9]{32}", parent)
        or type(index) is not int
        or index not in (0, 1)
    ):
        raise PipelineError("invalid ASR parent or part index")
    return hashlib.sha256(
        f"clipforge-asr-part-v1:{parent}:{index}".encode()
    ).hexdigest()[:32]


def prepare(source, root, request, settings, info):
    """Decode once on the hosted CPU; each lossless part includes boundary context."""
    if settings.asr_parts != 2 or not info["audio"]:
        raise PipelineError("split ASR requires audio and asr_parts=2")
    if (
        digest(source) != request["video_sha256"]
        or source.stat().st_size != request["video_bytes"]
    ):
        raise PipelineError("split input checksum or size mismatch")
    if not 0 < info["duration"] <= settings.max_source_seconds:
        raise PipelineError("split input duration exceeds the analysis budget")
    root.mkdir(parents=True, mode=0o700)
    wav = root / "audio.wav"
    try:
        media.audio(source, wav, info["duration"])
        with wave.open(str(wav), "rb") as handle:
            rate, frames = handle.getframerate(), handle.getnframes()
            if rate != media.ASR_SAMPLE_RATE or frames < 2:
                raise PipelineError(
                    "split audio must contain at least two samples at 16 kHz"
                )
        middle = frames // 2
        offset = float(info.get("audio_start", 0))
        boundary = middle / rate + offset
        if not 0 < boundary < info["duration"]:
            raise PipelineError("split audio has an invalid midpoint")
        plan = {
            "version": 1,
            "request": request,
            "settings": settings.model_dump(),
            "info": info,
            "transcript_key": pipeline.transcript_identity(source, info, settings),
            "boundary": boundary,
            "parts": [],
        }
        context = OVERLAP_SECONDS * rate
        for index, (first, last) in enumerate(
            ((0, min(frames, middle + context)), (max(0, middle - context), frames))
        ):
            directory = root / f"part-{index}"
            directory.mkdir(mode=0o700)
            audio = directory / "input-video.bin"
            process(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    *media.PROTOCOLS,
                    "-ss",
                    str(first / rate),
                    "-i",
                    wav,
                    "-t",
                    str((last - first) / rate),
                    "-map",
                    "0:a:0",
                    "-c:a",
                    "flac",
                    "-f",
                    "flac",
                    audio,
                ],
                timeout=max(300, info["duration"]),
                stage="asr-split-audio",
            )
            if not 0 < audio.stat().st_size <= settings.max_input_bytes:
                raise PipelineError("ASR part exceeds the input size limit")
            part = {
                "parent_request_id": request["request_id"],
                "index": index,
                "count": 2,
                "source_sha256": request["video_sha256"],
                "transcript_key": plan["transcript_key"],
                "offset": first / rate + offset,
                "duration": (last - first) / rate,
                "core_start": 0 if index == 0 else boundary,
                "core_end": boundary if index == 0 else info["duration"],
                "source_duration": info["duration"],
            }
            child = {
                **request,
                "request_id": part_id(request["request_id"], index),
                "video_sha256": digest(audio),
                "video_bytes": audio.stat().st_size,
                "settings": settings.model_copy(update={"asr_parts": 1}).model_dump(),
                "asr_part": part,
            }
            write_json(directory / "request.json", child)
            plan["parts"].append(child)
        plan["key"] = fingerprint(plan)
        write_json(root / "plan.json", plan)
        return plan
    finally:
        wav.unlink(missing_ok=True)


def validate_part(part):
    fields = {
        "parent_request_id",
        "index",
        "count",
        "source_sha256",
        "transcript_key",
        "offset",
        "duration",
        "core_start",
        "core_end",
        "source_duration",
    }
    if not isinstance(part, dict) or set(part) != fields:
        raise PipelineError("invalid ASR part descriptor")
    part_id(part["parent_request_id"], part["index"])
    if (
        type(part["count"]) is not int
        or part["count"] != 2
        or any(
            not isinstance(part.get(key), str)
            or not re.fullmatch(r"[a-f0-9]{64}", part[key])
            for key in ("source_sha256", "transcript_key")
        )
    ):
        raise PipelineError("invalid ASR part identity")
    if any(
        type(part[key]) not in (int, float) or not math.isfinite(part[key])
        for key in ("offset", "duration", "core_start", "core_end", "source_duration")
    ) or not (
        part["offset"] >= -TIME_TOLERANCE
        and part["duration"] > 0
        and 0 <= part["core_start"] < part["core_end"] <= part["source_duration"]
        and part["offset"] <= part["core_start"] + TIME_TOLERANCE
        and part["offset"] + part["duration"] >= part["core_end"] - 0.3
    ):
        raise PipelineError("invalid ASR part interval")


def merge(parts, duration):
    """Keep each word once, by the half-open ownership interval of its midpoint."""
    if len(parts) != 2:
        raise PipelineError("both ASR parts are required")
    ordered = sorted(parts, key=lambda row: row[0]["index"])
    for descriptor, _ in ordered:
        validate_part(descriptor)
    first, second = (row[0] for row in ordered)
    if (
        first["index"] != 0
        or second["index"] != 1
        or any(
            first[key] != second[key]
            for key in (
                "parent_request_id",
                "source_sha256",
                "transcript_key",
                "source_duration",
            )
        )
        or first["core_start"] != 0
        or first["core_end"] != second["core_start"]
        or second["core_end"] != duration
    ):
        raise PipelineError("ASR parts do not cover the same source exactly once")
    results, languages, previous_word = [], set(), None
    for descriptor, raw in ordered:
        transcript = Transcript.model_validate(raw)
        languages.add(transcript.language)
        lo, hi, offset = (
            descriptor[key] for key in ("core_start", "core_end", "offset")
        )
        boundary_word = previous_word
        for segment in transcript.segments:
            if segment.end > descriptor["duration"] + 0.3:
                raise PipelineError("ASR part transcript exceeds its audio")
            words = []
            for word in segment.words:
                if (
                    word.start < segment.start - TIME_TOLERANCE
                    or word.end > segment.end + TIME_TOLERANCE
                ):
                    raise PipelineError("ASR word exceeds its segment")
                start, end = (
                    max(0, word.start + offset),
                    min(duration, word.end + offset),
                )
                midpoint = (start + end) / 2
                if not (end > start and lo <= midpoint < hi):
                    continue
                item = Word(start=start, end=end, text=word.text)
                if (
                    descriptor["index"] == 1
                    and boundary_word
                    and item.start < boundary_word.end
                ):
                    if item.text.strip() == boundary_word.text.strip():
                        continue
                    if boundary_word.end - item.start > TIME_TOLERANCE:
                        raise PipelineError(
                            "ambiguous ASR split boundary; use --asr-parts 1"
                        )
                words.append(item)
                previous_word = item
            if words:
                results.append(
                    Segment(
                        start=words[0].start,
                        end=words[-1].end,
                        text="".join(word.text for word in words),
                        words=words,
                    )
                )
            elif not segment.words and segment.text.strip():
                start, end = (
                    max(0, segment.start + offset),
                    min(duration, segment.end + offset),
                )
                if lo <= start < end <= hi:
                    results.append(Segment(start=start, end=end, text=segment.text))
                elif start < hi and end > lo:
                    raise PipelineError(
                        "word timestamps are required at the ASR split boundary"
                    )
    if len(languages - {"und"}) > 1:
        raise PipelineError(
            "ASR parts detected different languages; set language explicitly"
        )
    results.sort(key=lambda segment: (segment.start, segment.end))
    return Transcript(
        language=next(iter(languages - {"und"}), "und"), segments=results
    ).model_dump()
