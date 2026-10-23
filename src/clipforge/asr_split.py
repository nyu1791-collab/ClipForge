"""Two to four independently recognised audio parts, joined on the original audio clock."""

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
MIN_PARTS, MAX_PARTS = 2, 4


def part_id(parent, index, count, split):
    if (
        not re.fullmatch(r"[a-f0-9]{32}", parent)
        or type(count) is not int
        or not MIN_PARTS <= count <= MAX_PARTS
        or type(index) is not int
        or not 0 <= index < count
        or not isinstance(split, str)
        or not re.fullmatch(r"[a-f0-9]{64}", split)
    ):
        raise PipelineError("invalid ASR parent, part index or split identity")
    return hashlib.sha256(
        f"clipforge-asr-part-v3:{parent}:{count}:{index}:{split}".encode()
    ).hexdigest()[:32]


def split_identity(split):
    """What decides the cuts, and so the audio and transcript; reasons and timings are excluded."""
    return {
        "parts": split["parts"],
        "search_seconds": split["search_seconds"],
        "cuts": [
            {key: cut[key] for key in ("method", "window", "silence", "sample")}
            for cut in split["cuts"]
        ],
    }


def split_key(split):
    return fingerprint(split_identity(split))


def choose_cuts(handle, rate, frames, offset, search_seconds, count):
    """One cut per boundary between neighbouring parts, each at the longest silence near its share.

    Cut k aims at k/count of the audio and searches search_seconds either side. Its window keeps
    at least half of an even share from the previous cut and from the end, so every part keeps
    at least half its share. Cuts are chosen in order, so each window sees the previous cut.
    """
    if not MIN_PARTS <= count <= MAX_PARTS:
        raise PipelineError("split ASR supports 2 to 4 parts")
    minimum = frames // (2 * count)
    reach = search_seconds * rate
    cuts, previous = [], 0
    for index in range(1, count):
        aim = frames * index // count
        lo = max(aim - reach, previous + minimum)
        hi = min(aim + reach, frames - (count - index) * minimum)
        cut = _cut_near(handle, rate, offset, search_seconds, aim, lo, hi)
        cuts.append(cut)
        previous = cut["sample"]
    return {"parts": count, "search_seconds": search_seconds, "cuts": cuts}


def _cut_near(handle, rate, offset, search_seconds, aim, lo, hi):
    """The longest silence between speech spans in [lo, hi), else the aim."""
    started = time.monotonic()

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
        return finish("middle", "search_disabled", aim)
    if lo >= hi:
        return finish("middle", "no_window", aim)
    try:
        from faster_whisper import vad
        import numpy as np

        importlib.import_module("onnxruntime")
    except ImportError:
        return finish("middle", "vad_unavailable", aim)
    handle.setpos(lo)
    pcm = handle.readframes(hi - lo)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    del pcm
    samples *= 1 / 32768.0
    try:
        spans = vad.get_speech_timestamps(samples, vad.VadOptions(), sampling_rate=rate)
    except Exception:
        return finish("middle", "vad_failed", aim)
    gaps = [
        (after["start"] - before["end"], lo + before["end"], lo + after["start"])
        for before, after in zip(spans, spans[1:])
        if after["start"] > before["end"]
    ]
    if not gaps:
        return finish("middle", "no_silence", aim)
    _, start, end = max(
        gaps, key=lambda gap: (gap[0], -abs((gap[1] + gap[2]) / 2 - aim))
    )
    return finish(
        "vad",
        "longest_silence",
        (start + end) // 2,
        silence=[start / rate + offset, end / rate + offset],
    )


def prepare(source, root, request, settings, info):
    """Decode once on the hosted CPU; each lossless part includes context around its cuts."""
    count = settings.asr_parts
    if not MIN_PARTS <= count <= MAX_PARTS or not info["audio"]:
        raise PipelineError("split ASR requires audio and 2 to 4 parts")
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
            if rate != media.ASR_SAMPLE_RATE or frames < 2 * count:
                raise PipelineError(
                    "split audio must contain at least two samples per part at 16 kHz"
                )
            offset = float(info.get("audio_start", 0))
            split = choose_cuts(
                handle,
                rate,
                frames,
                offset,
                settings.asr_split_search_seconds,
                count,
            )
        cuts = [cut["sample"] for cut in split["cuts"]]
        boundaries = [cut / rate + offset for cut in cuts]
        if (
            not 0 < boundaries[0]
            or not boundaries[-1] < info["duration"]
            or any(b >= c for b, c in zip(boundaries, boundaries[1:]))
        ):
            raise PipelineError("split audio has an invalid cut")
        identity = split_identity(split)
        plan = {
            "version": 3,
            "request": request,
            "settings": settings.model_dump(),
            "info": info,
            "transcript_key": pipeline.transcript_identity(
                source, info, settings, split=identity
            ),
            "boundaries": boundaries,
            "split": split,
            "parts": [],
        }
        context = OVERLAP_SECONDS * rate
        edges = [0, *cuts, frames]
        for index in range(count):
            first = max(0, edges[index] - context)
            last = min(frames, edges[index + 1] + context)
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
                "count": count,
                "source_sha256": request["video_sha256"],
                "transcript_key": plan["transcript_key"],
                "offset": first / rate + offset,
                "duration": (last - first) / rate,
                "core_start": 0 if index == 0 else boundaries[index - 1],
                "core_end": (
                    info["duration"] if index == count - 1 else boundaries[index]
                ),
                "source_duration": info["duration"],
                "split_key": split_key(split),
            }
            child = {
                **request,
                "request_id": part_id(
                    request["request_id"], index, count, part["split_key"]
                ),
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
    if (
        type(part.get("count")) is not int
        or not MIN_PARTS <= part["count"] <= MAX_PARTS
        or type(part.get("index")) is not int
        or not 0 <= part["index"] < part["count"]
    ):
        raise PipelineError("invalid ASR part index or count")
    part_id(
        part["parent_request_id"], part["index"], part["count"], part.get("split_key")
    )
    if any(
        not isinstance(part.get(key), str)
        or not re.fullmatch(r"[a-f0-9]{64}", part[key])
        for key in ("source_sha256", "transcript_key", "split_key")
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

    A word whose midpoint lies just outside its own part, within BOUNDARY_RESCUE of one of that
    part's cuts, is only a candidate. It is kept when no accepted word of another part overlaps
    it, so a word recognised on one side alone is not lost; an earlier part's candidate wins a
    clash. At each cut, the next part's first words yield to the previous part's last word.
    """
    count = len(parts)
    if not MIN_PARTS <= count <= MAX_PARTS:
        raise PipelineError("all ASR parts are required")
    ordered = sorted(parts, key=lambda row: row[0]["index"])
    for descriptor, _ in ordered:
        validate_part(descriptor)
    descriptors = [row[0] for row in ordered]
    if (
        [row["index"] for row in descriptors] != list(range(count))
        or any(row["count"] != count for row in descriptors)
        or any(
            descriptors[0][key] != row[key]
            for row in descriptors
            for key in (
                "parent_request_id",
                "source_sha256",
                "transcript_key",
                "source_duration",
            )
        )
        or descriptors[0]["core_start"] != 0
        or any(
            left["core_end"] != right["core_start"]
            for left, right in zip(descriptors, descriptors[1:])
        )
        or descriptors[-1]["core_end"] != duration
    ):
        raise PipelineError("ASR parts do not cover the same source exactly once")
    languages, results = set(), []
    # Per part: (segment position, word, accepted) for every word inside or near its interval.
    timed = {index: [] for index in range(count)}
    for descriptor, raw in ordered:
        index = descriptor["index"]
        transcript = Transcript.model_validate(raw)
        languages.add(transcript.language)
        lo, hi, offset = (
            descriptor[key] for key in ("core_start", "core_end", "offset")
        )
        cuts = [
            value
            for value, present in (
                (descriptor["core_start"], index > 0),
                (descriptor["core_end"], index < count - 1),
            )
            if present
        ]
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
                elif any(abs(midpoint - cut) <= BOUNDARY_RESCUE for cut in cuts):
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

    owned = {
        index: [item for _, item, kept in timed[index] if kept]
        for index in range(count)
    }
    rescued = {index: [] for index in range(count)}
    for index in range(count):
        # Candidates yield to words other parts own, and to earlier parts' rescued words.
        peers = [
            item for other in range(count) if other != index for item in owned[other]
        ] + [item for earlier in range(index) for _, item in rescued[earlier]]
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
        for index in range(count)
    }
    # The cut checks run on the final word lists, so they also cover rescued words.
    for left in range(count - 1):
        if not accepted[left]:
            continue
        boundary_word = accepted[left][-1][1]
        checked = []
        for position, item in accepted[left + 1]:
            if item.start < boundary_word.end:
                if item.text.strip() == boundary_word.text.strip():
                    continue
                if boundary_word.end - item.start > TIME_TOLERANCE:
                    raise PipelineError(
                        "ambiguous ASR split boundary; use --asr-parts 1"
                    )
            checked.append((position, item))
        accepted[left + 1] = checked
    for index in range(count):
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
