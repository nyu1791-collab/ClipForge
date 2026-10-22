"""Conservative, caption-only ASR cleanup in original-video seconds.

Inspired by Barański et al., Bag of Hallucinations (arXiv:2501.11378), and WhisperJAV.
"""

import re
import zlib

from .models import Segment


BOILERPLATE = {
    "ご視聴ありがとうございました",
    "チャンネル登録よろしくお願いします",
    "字幕は自動生成されています",
}
EDGE_MARKS = " \t\r\n。.!！?？…、,「」『』\"'"
WORD_REPEAT = re.compile(r"(?<!\w)(?P<unit>\w+)(?:\s+(?P=unit)){3,}(?!\w)")
CHAR_REPEAT = re.compile(r"(?P<unit>\S{2,6}?)(?P=unit){3,}")


def _repetition_mask(text, words=()):
    """Keep mask and removed occurrence count; prefer the longest non-overlapping runs."""
    runs = []
    for match in WORD_REPEAT.finditer(text):
        units = list(re.finditer(r"\S+", match.group()))
        runs.append(
            (match.start(), match.end(), match.start() + units[1].end(), len(units) - 2)
        )
    for match in CHAR_REPEAT.finditer(text):
        width = len(match["unit"])
        runs.append(
            (
                match.start(),
                match.end(),
                match.start() + 2 * width,
                len(match[0]) // width - 2,
            )
        )

    # ASR token boundaries also identify one-character and longer-than-six-character words.
    offsets, cursor = [], 0
    for word in words:
        offsets.append(cursor)
        cursor += len(word.text)
    index = 0
    while index < len(words):
        stop = index + 1
        unit = words[index].text.strip()
        while unit and stop < len(words) and words[stop].text.strip() == unit:
            stop += 1
        if stop - index >= 4:
            end = offsets[stop - 1] + len(words[stop - 1].text)
            runs.append((offsets[index], end, offsets[index + 2], stop - index - 2))
        index = stop

    keep, removed, previous_end = [True] * len(text), 0, 0
    for start, end, cut, count in sorted(
        runs, key=lambda run: (run[0], -run[1], run[2])
    ):
        if start < previous_end:
            continue
        keep[cut:end] = [False] * (end - cut)
        removed += count
        previous_end = end
    return keep, removed


def _kept_text(text, keep):
    return "".join(char for char, retain in zip(text, keep) if retain)


def _segment_text(text, source, keep):
    if text == source:
        return _kept_text(text, keep)
    source_chars = [
        (char, retain) for char, retain in zip(source, keep) if not char.isspace()
    ]
    if "".join(char for char, _ in source_chars) != "".join(text.split()):
        # A pre-existing text/token mismatch must not replace text with a different transcript.
        text_keep, _ = _repetition_mask(text)
        return _kept_text(text, text_keep)
    flags = iter(retain for _, retain in source_chars)
    text_keep = [next(flags) if not char.isspace() else None for char in text]
    following = True
    for index in range(len(text) - 1, -1, -1):
        if text_keep[index] is None:
            text_keep[index] = following
        else:
            following = text_keep[index]
    return _kept_text(text, text_keep)


def _kept_words(words, keep):
    result, offset = [], 0
    for word in words:
        flags = keep[offset : offset + len(word.text)]
        offset += len(word.text)
        if all(flags):
            result.append(word)
            continue
        index = 0
        while index < len(flags):
            if not flags[index]:
                index += 1
                continue
            stop = index + 1
            while stop < len(flags) and flags[stop]:
                stop += 1
            if word.text[index:stop].strip():
                duration = word.end - word.start
                result.append(
                    word.model_copy(
                        update={
                            "text": word.text[index:stop],
                            "start": word.start + duration * index / len(word.text),
                            "end": word.start + duration * stop / len(word.text),
                        }
                    )
                )
            index = stop
    return result


def clean_segments(segments: list[Segment]) -> tuple[list[Segment], dict[str, int]]:
    """Return new segments and counts, without mutating the input or rebasing time.

    ``repetitions`` counts removed occurrences; the other counts are whole segments.
    Whole-segment checks precede trimming, so compression is measured on the original.
    """
    cleaned = []
    counts = {"repetitions": 0, "boilerplate_segments": 0, "compression_segments": 0}
    for segment in segments:
        # Bag of Hallucinations / WhisperJAV: only whole, known silence-prone boilerplate.
        if segment.text.strip(EDGE_MARKS) in BOILERPLATE:
            counts["boilerplate_segments"] += 1
            continue
        # Bag of Hallucinations / WhisperJAV motivate a conservative repetitive-output filter.
        encoded = segment.text.encode("utf-8")
        if len(segment.text) >= 20 and len(zlib.compress(encoded)) / len(encoded) < 0.2:
            counts["compression_segments"] += 1
            continue
        # Repetition loops are an ASR hallucination signal; keep two, leaving natural 2–3 runs.
        source = (
            "".join(word.text for word in segment.words)
            if segment.words
            else segment.text
        )
        keep, removed = _repetition_mask(source, segment.words)
        counts["repetitions"] += removed
        if removed:
            cleaned.append(
                segment.model_copy(
                    update={
                        "text": _segment_text(segment.text, source, keep),
                        "words": _kept_words(segment.words, keep),
                    }
                )
            )
        else:
            cleaned.append(segment.model_copy(deep=True))
    return cleaned, counts
