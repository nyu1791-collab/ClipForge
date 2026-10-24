"""One to four audio parts recognised on separate Colab runtimes, joined on the source clock.

The runner decodes the audio once and cuts it at the quietest moment near each even share,
without overlap, so every word belongs to exactly one part. Each part is recognised on its
own runtime (asr.py), and the parts' segments are shifted by their start and concatenated.
"""

import hashlib
from pathlib import Path
import re
import wave

from . import asr, media
from .models import Transcript
from .storage import PipelineError, digest, fingerprint, write_json

MIN_PARTS, MAX_PARTS = 1, 4
# Each cut is the quietest moment within this many seconds of an even share.
SPLIT_SEARCH_SECONDS = 60
PART_FIELDS = {
    "parent_request_id",
    "index",
    "count",
    "source_sha256",
    "transcript_key",
    "split_key",
    "start",
    "end",
}


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
        f"clipforge-asr-part-v4:{parent}:{count}:{index}:{split}".encode()
    ).hexdigest()[:32]


def choose_cuts(handle, count):
    """Sample offsets of the cuts between neighbouring parts.

    Cut k aims at k/count of the audio and takes the quietest moment within
    SPLIT_SEARCH_SECONDS of it, keeping at least half an even share in every part.
    """
    import numpy as np

    rate, frames = handle.getframerate(), handle.getnframes()
    minimum = frames // (2 * count)
    reach = SPLIT_SEARCH_SECONDS * rate
    cuts, previous = [], 0
    for index in range(1, count):
        aim = frames * index // count
        lo = max(aim - reach, previous + minimum)
        hi = min(aim + reach, frames - (count - index) * minimum)
        if hi - lo <= int(asr.WINDOW_SECONDS * rate):
            cut = aim
        else:
            handle.setpos(lo)
            window = np.frombuffer(handle.readframes(hi - lo), dtype="<i2")
            cut = lo + asr.quietest(window.astype(np.float32) / 32768.0, rate)
        cuts.append(cut)
        previous = cut
    return cuts


def transcript_key(source_sha256, info, settings, split):
    """Identity of a recognised transcript: its audio, recogniser, code and cuts."""
    return fingerprint(
        {
            "source_sha256": source_sha256,
            "duration": info["duration"],
            "audio_start": info.get("audio_start", 0.0),
            "language": settings.language,
            "model": asr.MODEL,
            "nemo": asr.NEMO,
            "code": {
                name: digest(Path(__file__).parent / name)
                for name in ("asr.py", "asr_split.py")
            },
            "split": split,
        }
    )


def prepare(source, root, request, settings, info):
    """Decode once on the runner and write each part as a 16 kHz mono WAV."""
    count = settings.asr_parts
    if not MIN_PARTS <= count <= MAX_PARTS or not info["audio"]:
        raise PipelineError("ASR requires audio and 1 to 4 parts")
    if (
        digest(source) != request["video_sha256"]
        or source.stat().st_size != request["video_bytes"]
    ):
        raise PipelineError("ASR input checksum or size mismatch")
    if not 0 < info["duration"] <= settings.max_source_seconds:
        raise PipelineError("ASR input duration exceeds the analysis budget")
    root.mkdir(parents=True, mode=0o700)
    wav = root / "audio.wav"
    try:
        media.audio(source, wav, info["duration"])
        with wave.open(str(wav), "rb") as handle:
            asr.check_format(handle)
            frames = handle.getnframes()
            if frames < 2 * count:
                raise PipelineError("ASR audio is too short for its parts")
            cuts = choose_cuts(handle, count)
            edges = [0, *cuts, frames]
            if any(a >= b for a, b in zip(edges, edges[1:])):
                raise PipelineError("ASR audio has an invalid cut")
            split = {
                "parts": count,
                "search_seconds": SPLIT_SEARCH_SECONDS,
                "cuts": cuts,
            }
            key = fingerprint(split)
            offset = float(info.get("audio_start", 0.0))
            plan = {
                "version": 4,
                "request": request,
                "settings": settings.model_dump(),
                "info": info,
                "transcript_key": transcript_key(
                    request["video_sha256"], info, settings, split
                ),
                "split": split,
                "split_key": key,
                "parts": [],
            }
            for index in range(count):
                directory = root / f"part-{index}"
                directory.mkdir(mode=0o700)
                audio = directory / "audio.wav"
                handle.setpos(edges[index])
                with wave.open(str(audio), "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(asr.SAMPLE_RATE)
                    out.writeframes(handle.readframes(edges[index + 1] - edges[index]))
                part = {
                    "parent_request_id": request["request_id"],
                    "index": index,
                    "count": count,
                    "source_sha256": request["video_sha256"],
                    "transcript_key": plan["transcript_key"],
                    "split_key": key,
                    "start": offset + edges[index] / asr.SAMPLE_RATE,
                    "end": offset + edges[index + 1] / asr.SAMPLE_RATE,
                }
                child = {
                    "request_id": part_id(request["request_id"], index, count, key),
                    "source_ref": request["source_ref"],
                    "audio_sha256": digest(audio),
                    "audio_bytes": audio.stat().st_size,
                    "language": settings.language or "ja",
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
    if not isinstance(part, dict) or set(part) != PART_FIELDS:
        raise PipelineError("invalid ASR part descriptor")
    part_id(part["parent_request_id"], part["index"], part["count"], part["split_key"])
    if any(
        not isinstance(part[key], str) or not re.fullmatch(r"[a-f0-9]{64}", part[key])
        for key in ("source_sha256", "transcript_key", "split_key")
    ):
        raise PipelineError("invalid ASR part identity")
    if (
        any(type(part[key]) not in (int, float) for key in ("start", "end"))
        or not 0 <= part["start"] < part["end"]
    ):
        raise PipelineError("invalid ASR part interval")


def merge(parts, duration):
    """Shift each part's segments by its start and join them in order."""
    ordered = sorted(parts, key=lambda row: row[0]["index"])
    for descriptor, _ in ordered:
        validate_part(descriptor)
    descriptors = [row[0] for row in ordered]
    count = len(descriptors)
    if (
        not MIN_PARTS <= count <= MAX_PARTS
        or [row["index"] for row in descriptors] != list(range(count))
        or any(row["count"] != count for row in descriptors)
        or any(
            descriptors[0][key] != row[key]
            for row in descriptors
            for key in ("parent_request_id", "source_sha256", "transcript_key")
        )
        or any(
            abs(left["end"] - right["start"]) > 1e-6
            for left, right in zip(descriptors, descriptors[1:])
        )
    ):
        raise PipelineError("ASR parts do not cover the same source exactly once")
    languages, segments = set(), []
    for descriptor, raw in ordered:
        transcript = Transcript.model_validate(raw)
        languages.add(transcript.language)
        start, length = descriptor["start"], descriptor["end"] - descriptor["start"]
        for segment in transcript.segments:
            if segment.end > length + 0.3:
                raise PipelineError("ASR part transcript exceeds its audio")
            value = segment.model_dump()
            value["start"] += start
            value["end"] = min(value["end"] + start, duration)
            value["words"] = [
                {**word, "start": word["start"] + start, "end": end}
                for word in value["words"]
                if (end := min(word["end"] + start, duration)) > word["start"] + start
            ]
            if value["end"] > value["start"]:
                segments.append(value)
    if len(languages) != 1:
        raise PipelineError("ASR parts reported different languages")
    return Transcript(language=languages.pop(), segments=segments).model_dump()
