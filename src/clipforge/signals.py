"""CPU transcription and optional chat signals share original-video seconds."""

import bisect
import math
from pathlib import Path
import re
import tempfile
import wave

from . import media
from .models import Candidate, Segment, Transcript, Word
from .storage import PipelineError


def read_samples(path):
    """Mono 16 kHz 16-bit WAV as float32 samples in [-1, 1)."""
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
            1,
            2,
            16000,
        ):
            raise PipelineError("speech audio must be mono 16-bit PCM at 16 kHz")
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def transcribe(source, info, settings):
    if not info["audio"]:
        return Transcript(language="und", segments=[]).model_dump()
    from faster_whisper import WhisperModel

    with tempfile.TemporaryDirectory(prefix="clipforge-asr-") as temporary:
        path = Path(temporary) / "audio.wav"
        media.audio(source, path, info["duration"])
        model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",
            cpu_threads=settings.threads,
            num_workers=1,
            revision=settings.whisper_revision,
        )
        # Samples, not a path: faster-whisper would otherwise decode with PyAV, whose
        # open() arguments changed in PyAV 19 (the locked build for Python 3.12+).
        segments, metadata = model.transcribe(
            read_samples(path),
            language=settings.language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        output = []
        # The extracted WAV starts where the audio stream starts; shift back to the file's clock.
        offset = float(info.get("audio_start", 0.0))
        for segment in segments:
            start = max(0, segment.start + offset)
            end = min(info["duration"], segment.end + offset)
            if end <= start:
                continue
            words = []
            for word in segment.words or []:
                ws = max(start, word.start + offset)
                we = min(end, word.end + offset)
                if we > ws and word.word.strip():
                    words.append(Word(start=ws, end=we, text=word.word))
            output.append(Segment(start=start, end=end, text=segment.text, words=words))
        return Transcript(
            language=metadata.language or "und", segments=output
        ).model_dump()


def scene_boundaries(source, duration):
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector

    video = open_video(str(source))
    manager = SceneManager()
    manager.auto_downscale = True
    manager.add_detector(AdaptiveDetector(min_scene_len=15))
    manager.detect_scenes(video=video, frame_skip=2, show_progress=False)
    scenes = manager.get_scene_list(start_in_scene=True)
    boundaries = sorted(
        {
            round(t.get_seconds(), 6)
            for pair in scenes
            for t in pair
            if 0 < t.get_seconds() < duration
        }
    )
    return {"boundaries": boundaries, "detector": "AdaptiveDetector", "frame_skip": 2}


def chat_signals(duration, settings, supplied=None):
    events = []
    status = "not_requested"
    client = None
    try:
        if supplied is not None:
            if not isinstance(supplied, list) or len(supplied) > 100000:
                raise PipelineError(
                    "chat JSON must be a list with at most 100000 entries"
                )
            messages, status = supplied, "provided"
        elif settings.chat_url:
            from chat_downloader import ChatDownloader

            client = ChatDownloader()
            messages = client.get_chat(
                settings.chat_url,
                start_time=0,
                end_time=duration + max(0, -settings.chat_offset_seconds),
                timeout=120,
                max_messages=100000,
                max_attempts=1,
            )
            status = "downloaded"
        else:
            messages = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            value = message.get("time_in_seconds", message.get("time"))
            try:
                seconds = float(value) + settings.chat_offset_seconds
            except (ValueError, TypeError):
                continue
            if not math.isfinite(seconds) or not 0 <= seconds <= duration:
                continue
            text = str(message.get("message", ""))
            reaction = bool(
                re.search(r"草|[wｗ]{2,}|\blol\b|\blmao\b|😂|🤣", text, re.I)
            )
            events.append({"time": seconds, "weight": 2.0 if reaction else 1.0})
    except Exception as error:
        # A missing/unsupported replay is optional. Preserve collected timestamps only.
        if supplied is not None:
            raise PipelineError("invalid supplied chat data") from None
        status = "unavailable_" + type(error).__name__
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    events.sort(key=lambda e: e["time"])
    return {
        "status": status,
        "events": events,
        "offset_seconds": settings.chat_offset_seconds,
    }


def _covered_seconds(intervals):
    """Seconds covered by the union of intervals, so overlapping speech is not counted twice."""
    total, run_start, run_end = 0.0, None, None
    for a, b in sorted(intervals):
        if run_end is None or a > run_end:
            if run_end is not None:
                total += run_end - run_start
            run_start, run_end = a, b
        else:
            run_end = max(run_end, b)
    if run_end is not None:
        total += run_end - run_start
    return total


def candidates(info, transcript, scenes, chat, settings, topics=None):
    duration = info["duration"]
    if duration < settings.min_seconds:
        raise PipelineError("video is shorter than the requested minimum clip length")
    window = min(duration, settings.max_seconds)
    step = max(5, window / 2)
    topic_spans = topics or [(0.0, duration)]
    speech_starts = [s.start for s in transcript.segments]
    speech_ends = [s.end for s in transcript.segments]
    event_times = [e["time"] for e in chat["events"]]
    event_prefix = [0.0]
    for event in chat["events"]:
        event_prefix.append(event_prefix[-1] + event["weight"])
    baseline = event_prefix[-1] / max(duration, 1)
    starts = {
        round(min(i * step, max(0, duration - window)), 4)
        for i in range(int(duration / step) + 1)
    }
    # Context before a reaction peak is useful; no chat is required to produce candidates.
    counts = {}
    for event in chat["events"]:
        bucket = int(event["time"] // 5)
        counts[bucket] = counts.get(bucket, 0) + event["weight"]
    peaks = sorted(counts, key=counts.get, reverse=True)[: settings.candidates * 4]
    for bucket in peaks:
        starts.add(round(max(0, min(duration - window, bucket * 5 - window * 0.65)), 4))
    # Each topic contributes its own window starts, so short topics are not lost.
    for a, b in topic_spans:
        span_start = max(0.0, min(a, duration - window))
        for k in range(int(max(0.0, b - a - window) / step) + 1):
            starts.add(round(min(span_start + k * step, max(0, duration - window)), 4))
    pool = []
    for initial in sorted(starts):
        start = initial
        # Preserve utterance boundaries when nearby; original timestamps never change.
        near = [v for v in speech_starts if abs(v - start) <= 3]
        if near:
            start = min(near, key=lambda v: abs(v - start))
        end = min(duration, start + window)
        near = [v for v in speech_ends if end - 4 <= v <= end]
        if near:
            end = max(near)
        boundaries = scenes["boundaries"]
        near = [v for v in boundaries if end - 2 <= v <= end]
        if near:
            end = max(near)
        if end - start < settings.min_seconds:
            # Boundary snapping must not discard a legal short/tail video.
            start, end = initial, min(duration, initial + window)
            if end - start < settings.min_seconds:
                continue
        speech = _covered_seconds(
            [
                (max(s.start, start), min(s.end, end))
                for s in transcript.segments
                if min(s.end, end) > max(s.start, start)
            ]
        )
        left, right = (
            bisect.bisect_left(event_times, start),
            bisect.bisect_right(event_times, end),
        )
        reactions = event_prefix[right] - event_prefix[left]
        density = reactions / max(1, end - start)
        burst = min(5, density / max(baseline, 0.01)) if reactions else 0
        cuts = bisect.bisect_right(boundaries, end) - bisect.bisect_left(
            boundaries, start
        )
        text = " ".join(
            s.text for s in transcript.segments if s.end > start and s.start < end
        )
        hooks = len(
            re.findall(
                r"まさか|すご|やば|なぜ|どうして|結論|実は|why|how|wow|finally",
                text,
                re.I,
            )
        )
        score = (
            min(1, speech / (end - start)) * 2
            + burst
            + min(hooks, 3) * 0.3
            + min(cuts, 3) * 0.05
        )
        pool.append(
            Candidate(
                id="candidate-000",
                start=start,
                end=end,
                heuristic_score=round(score, 4),
                speech_seconds=round(speech, 3),
                chat_reactions=reactions,
                scene_boundaries=cuts,
            )
        )
    ranked = sorted(pool, key=lambda c: (-c.heuristic_score, c.start))
    chosen = []

    def add(item):
        if all(
            max(0, min(c.end, item.end) - max(c.start, item.start))
            / min(c.end - c.start, item.end - item.start)
            <= 0.45
            for c in chosen
        ):
            chosen.append(item)
            return True
        return False

    def middle(item):
        return (item.start + item.end) / 2

    def spread(items, slots):
        """Best item of each equal time slot, taken in slot order when the overlap rule allows."""
        for slot in range(slots):
            center = duration * (slot + 0.5) / slots
            in_slot = [
                item
                for item in items
                if min(slots - 1, int(middle(item) / duration * slots)) == slot
            ]
            for item in sorted(
                in_slot, key=lambda c: (-c.heuristic_score, abs(middle(c) - center))
            ):
                if add(item):
                    break

    # Keep strong signals, plus temporal diversity so tied scores do not spend
    # the entire visual-analysis budget on the opening minutes.
    strong = max(1, settings.candidates // 2)
    for item in ranked:
        add(item)
        if len(chosen) >= strong:
            break
    remaining = settings.candidates - strong
    if topics:
        # Each topic's best window, then spread over the video so later topics are not dropped.
        heads = []
        for a, b in topic_spans:
            inside = [item for item in ranked if a <= middle(item) < b]
            # A topic shorter than a window has no window centred in it; any overlap will do.
            overlapping = [
                item for item in ranked if min(item.end, b) - max(item.start, a) > 0
            ]
            if inside or overlapping:
                heads.append((inside or overlapping)[0])
        spread(heads, remaining)
    else:
        spread(ranked, remaining)
    for item in ranked:
        if len(chosen) >= settings.candidates:
            break
        add(item)
    chosen = chosen[: settings.candidates]
    if not chosen:
        raise PipelineError("no valid candidate intervals could be constructed")
    for index, item in enumerate(chosen, 1):
        item.id = f"candidate-{index:03}"
    return [c.model_dump() for c in chosen]
