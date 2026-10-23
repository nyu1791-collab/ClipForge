"""Two independently recognised audio parts, joined on the original audio clock."""

import hashlib
import importlib
import math
import re
import time
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
BOUNDARY_RESCUE = 1.0


def part_id(parent, index, split):
    if (
        not re.fullmatch(r"[a-f0-9]{32}", parent)
        or type(index) is not int
        or index not in (0, 1)
        or not isinstance(split, str)
        or not re.fullmatch(r"[a-f0-9]{64}", split)
    ):
        raise PipelineError("invalid ASR parent, part index or split identity")
    return hashlib.sha256(
        f"clipforge-asr-part-v2:{parent}:{index}:{split}".encode()
    ).hexdigest()[:32]


def split_identity(split):
    """What decides the cut, and so the audio and transcript; reasons and timings are excluded."""
    return {
        key: split[key]
        for key in ("method", "search_seconds", "window", "silence", "sample")
    }


def split_key(split):
    return fingerprint(split_identity(split))


def choose_boundary(handle, rate, frames, offset, search_seconds):
    """Cut at the longest silence within search_seconds of the middle, else at the middle.

    Only the search window is read from the decoded 16 kHz WAV, once. The window stays at least
    a quarter of the audio away from both ends, so each part keeps at least a quarter.
    Silences are the gaps between VAD speech spans; ties go to the one nearest the middle.
    """
    started = time.monotonic()
    middle = frames // 2
    reach = search_seconds * rate
    lo = max(frames // 4, middle - reach)
    hi = min(frames - frames // 4, middle + reach)

    def finish(method, reason, sample, silence=None):
        return {
            "method": method,
            "reason": reason,
            "search_seconds": search_seconds,
            "window": [lo / rate + offset, hi / rate + offset],
            "silence": silence,
            "sample": sample,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    if search_seconds == 0:
        return finish("middle", "search_disabled", middle)
    try:
        from faster_whisper import vad
        import numpy as np

        importlib.import_module("onnxruntime")
    except ImportError:
        return finish("middle", "vad_unavailable", middle)
    handle.setpos(lo)
    pcm = handle.readframes(hi - lo)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    del pcm
    samples *= 1 / 32768.0
    try:
        spans = vad.get_speech_timestamps(samples, vad.VadOptions(), sampling_rate=rate)
    except Exception:
        return finish("middle", "vad_failed", middle)
    gaps = [
        (after["start"] - before["end"], lo + before["end"], lo + after["start"])
        for before, after in zip(spans, spans[1:])
        if after["start"] > before["end"]
    ]
    if not gaps:
        return finish("middle", "no_silence", middle)
    _, start, end = max(
        gaps, key=lambda gap: (gap[0], -abs((gap[1] + gap[2]) / 2 - middle))
    )
    return finish(
        "vad",
        "longest_silence",
        (start + end) // 2,
        silence=[start / rate + offset, end / rate + offset],
    )


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
            offset = float(info.get("audio_start", 0))
            split = choose_boundary(
                handle, rate, frames, offset, settings.asr_split_search_seconds
            )
        cut = split["sample"]
        boundary = cut / rate + offset
        if not 0 < boundary < info["duration"]:
            raise PipelineError("split audio has an invalid midpoint")
        identity = split_identity(split)
        plan = {
            "version": 2,
            "request": request,
            "settings": settings.model_dump(),
            "info": info,
            "transcript_key": pipeline.transcript_identity(
                source, info, settings, split=identity
            ),
            "boundary": boundary,
            "split": split,
            "parts": [],
        }
        context = OVERLAP_SECONDS * rate
        for index, (first, last) in enumerate(
            ((0, min(frames, cut + context)), (max(0, cut - context), frames))
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
                "split_key": split_key(split),
            }
            child = {
                **request,
                "request_id": part_id(request["request_id"], index, part["split_key"]),
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
        "split_key",
        "offset",
        "duration",
        "core_start",
        "core_end",
        "source_duration",
    }
    if not isinstance(part, dict) or set(part) != fields:
        raise PipelineError("invalid ASR part descriptor")
    part_id(part["parent_request_id"], part["index"], part.get("split_key"))
    if (
        type(part["count"]) is not int
        or part["count"] != 2
        or any(
            not isinstance(part.get(key), str)
            or not re.fullmatch(r"[a-f0-9]{64}", part[key])
            for key in ("source_sha256", "transcript_key", "split_key")
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
    """Keep each word once, by the half-open ownership interval of its midpoint.

    A word whose midpoint lies just outside its own part, within BOUNDARY_RESCUE of the cut, is
    only a candidate. It is kept when no accepted word of the other part overlaps it, so a word
    recognised on one side alone is not lost; the first part's candidate wins a clash.
    """
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
    boundary = first["core_end"]
    languages, results = set(), []
    # Per part: (segment position, word, accepted) for every word inside or near its interval.
    timed = {0: [], 1: []}
    for descriptor, raw in ordered:
        index = descriptor["index"]
        transcript = Transcript.model_validate(raw)
        languages.add(transcript.language)
        lo, hi, offset = (
            descriptor[key] for key in ("core_start", "core_end", "offset")
        )
        for position, segment in enumerate(transcript.segments):
            if segment.end > descriptor["duration"] + 0.3:
                raise PipelineError("ASR part transcript exceeds its audio")
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
                if not end > start:
                    continue
                item = Word(start=start, end=end, text=word.text)
                if lo <= midpoint < hi:
                    timed[index].append((position, item, True))
                elif abs(midpoint - boundary) <= BOUNDARY_RESCUE:
                    timed[index].append((position, item, False))
            if not segment.words and segment.text.strip():
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

    def overlaps(item, peers):
        return any(item.start < peer.end and item.end > peer.start for peer in peers)

    owned = {index: [item for _, item, kept in timed[index] if kept] for index in (0, 1)}
    rescued = {0: [], 1: []}
    for index in (0, 1):
        # The second part's candidates also yield to the first part's adopted candidates.
        peers = owned[1 - index] + [item for _, item in rescued[0]]
        rescued[index] = [
            (position, item)
            for position, item, kept in timed[index]
            if not kept and not overlaps(item, peers)
        ]
    accepted = {
        index: sorted(
            [(position, item) for position, item, kept in timed[index] if kept]
            + rescued[index],
            key=lambda pair: (pair[1].start, pair[1].end),
        )
        for index in (0, 1)
    }
    # The boundary check runs on the final word lists, so it also covers rescued words.
    if accepted[0]:
        boundary_word = accepted[0][-1][1]
        checked = []
        for position, item in accepted[1]:
            if item.start < boundary_word.end:
                if item.text.strip() == boundary_word.text.strip():
                    continue
                if boundary_word.end - item.start > TIME_TOLERANCE:
                    raise PipelineError(
                        "ambiguous ASR split boundary; use --asr-parts 1"
                    )
            checked.append((position, item))
        accepted[1] = checked
    for index in (0, 1):
        grouped = {}
        for position, item in accepted[index]:
            grouped.setdefault(position, []).append(item)
        results.extend(
            Segment(
                start=words[0].start,
                end=words[-1].end,
                text="".join(word.text for word in words),
                words=words,
            )
            for words in grouped.values()
        )
    if len(languages - {"und"}) > 1:
        raise PipelineError(
            "ASR parts detected different languages; set language explicitly"
        )
    results.sort(key=lambda segment: (segment.start, segment.end))
    return Transcript(
        language=next(iter(languages - {"und"}), "und"), segments=results
    ).model_dump()
