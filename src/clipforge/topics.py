"""Topic segmentation from speech timing and lexical cohesion (no model download).

Boundaries are gaps between utterances where the pause is long or the words on
both sides share little vocabulary. Duration targets never force a merge or split.
"""

from itertools import pairwise

CONTEXT_CHARS = 200


def _bigrams(text):
    compact = "".join(ch for ch in text if not ch.isspace())
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _similarity(left, right):
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _context_before(segments, index):
    text, used = "", 0
    for segment in reversed(segments[: index + 1]):
        text = segment.text + text
        used += len(segment.text)
        if used >= CONTEXT_CHARS:
            break
    return text[-CONTEXT_CHARS:]


def _context_after(segments, index):
    text = ""
    for segment in segments[index + 1 :]:
        text += segment.text
        if len(text) >= CONTEXT_CHARS:
            break
    return text[:CONTEXT_CHARS]


def boundary_strengths(segments):
    """(gap index, strength, pause, edge) for each gap between consecutive utterances.

    The pause runs from the latest speech end so far, so a short segment nested inside a
    longer one cannot make a gap look longer than the silence really is. The edge is the
    time that latest speech ends.
    """
    strengths = []
    edge = 0.0
    for index in range(len(segments) - 1):
        edge = max(edge, segments[index].end)
        pause = max(0.0, segments[index + 1].start - edge)
        similarity = _similarity(
            _context_before(segments, index), _context_after(segments, index)
        )
        strength = 0.5 * min(1.0, pause / 1.5) + 0.5 * (1.0 - similarity)
        strengths.append((index, strength, pause, edge))
    return strengths


def split(transcript, duration, min_seconds=300.0, max_seconds=900.0):
    """Return ordered (start, end) topic spans that cover [0, duration]."""
    duration = float(duration)
    segments = sorted(transcript.segments, key=lambda s: s.start)
    # Every gap is a possible split point (time, strength, pause); only some are cut now.
    gaps = [
        (edge + pause / 2, strength, pause)
        for index, strength, pause, edge in boundary_strengths(segments)
    ]
    cuts = {
        t: strength
        for t, strength, pause in gaps
        if pause >= 1.0 or (strength >= 0.7 and pause >= 0.3)
    }

    def points():
        return [0.0, *sorted(cuts), duration]

    bounds = sorted({round(p, 3) for p in points()})
    return [(a, b) for a, b in pairwise(bounds) if b - a > 0.05]
