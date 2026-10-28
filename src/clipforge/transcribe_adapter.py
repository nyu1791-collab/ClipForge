"""Convert Gemini Transcribe's word timings into selected-clip input for editing.

This adapter does not send requests. All times in the result are clip-relative.
"""

import math

from .storage import PipelineError


def _seconds(value):
    if isinstance(value, str) and value.endswith("s"):
        value = value[:-1]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def word_segments(response, offset, duration):
    """Keep every usable timed word, including words sharing the same timestamp.

    Speaker IDs returned by a transcription request are local to that request;
    they must not be assumed to identify the same person across chunks.
    Unaligned text is returned separately so callers can retain it without
    inventing a timestamp.
    """
    candidates = response.get("candidates") or []
    if not candidates or candidates[0].get("finishReason") != "STOP":
        raise PipelineError("Transcribe response did not complete")
    rows, untimed = [], []
    for part in candidates[0].get("content", {}).get("parts", []):
        transcription = part.get("audioTranscription") or {}
        words = transcription.get("words") or []
        if not words:
            text = part.get("text") or transcription.get("text") or ""
            if text.strip():
                untimed.append(text)
            continue
        for word in words:
            text = word.get("word")
            if not isinstance(text, str) or not text.strip():
                continue
            start = _seconds(word.get("startOffset"))
            end = _seconds(word.get("endOffset"))
            if start is None or end is None or end < start:
                untimed.append(text)
                continue
            start = max(0, min(duration, start + offset))
            end = max(0, min(duration, end + offset))
            if end <= start:
                if rows and rows[-1].get("source_speaker") == transcription.get(
                    "speakerLabel"
                ):
                    rows[-1]["text"] += text
                else:
                    untimed.append(text)
                continue
            rows.append(
                {
                    "start": start,
                    "end": end,
                    "text": text,
                    "source_speaker": transcription.get("speakerLabel"),
                }
            )
    return rows, untimed
