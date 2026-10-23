"""Pack captions for a reviewed clip; standard library only, so runners can import it."""

import base64
import json
import zlib


def pack_captions(segments):
    """Pack Transcript-shaped segments (source seconds) for a reviewed clip's captions.

    The result is base64 zlib JSON: [[start_cs, end_cs, text, [[word, start_cs, end_cs],
    ...]], ...] in source centiseconds; audio_first.caption_overrides reads it back.
    """

    def cs(seconds):
        return round(seconds * 100)

    rows = [
        [
            cs(s["start"]),
            cs(s["end"]),
            s["text"],
            [[w["text"], cs(w["start"]), cs(w["end"])] for w in s.get("words", [])],
        ]
        for s in segments
    ]
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(zlib.compress(raw, 9)).decode()


def unpack_captions(packed):
    """The [[start_cs, end_cs, text, [[word, start_cs, end_cs], ...]], ...] rows of packed captions."""
    return json.loads(zlib.decompress(base64.b64decode(packed, validate=True)))


def _repack(rows):
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(zlib.compress(raw, 9)).decode()


def apply_edits(packed, clip_start, edits):
    """Replace the caption text that each edit names near its clip time (seconds).

    The nearest line within ten seconds that holds the text is changed; the new text
    shares the time of the words it replaces. An edit that names no such text is refused.
    """
    rows = unpack_captions(packed)
    for edit in edits:
        before, after, at = edit["from"], edit["to"], float(edit["at"])
        lines = [
            row
            for row in rows
            if before in row[2] and abs(row[0] / 100 - clip_start - at) <= 10
        ]
        if not lines:
            raise ValueError(f"no caption near {at:.1f} s holds {before!r}")
        row = min(lines, key=lambda r: abs(r[0] / 100 - clip_start - at))
        words, offsets, position = row[3], [], 0
        for word in words:
            offsets.append(position)
            position += len(word[0])
        first = row[2].index(before)
        last = first + len(before)
        covered = [
            i
            for i, start in enumerate(offsets)
            if start < last and start + len(words[i][0]) > first
        ]
        if (
            not covered
            or offsets[covered[0]] != first
            or (offsets[covered[-1]] + len(words[covered[-1]][0]) != last)
        ):
            raise ValueError(f"{before!r} does not start and end on caption words")
        span_start, span_end = words[covered[0]][1], words[covered[-1]][2]
        step = (span_end - span_start) / max(1, len(after))
        new = [
            [char, round(span_start + i * step), round(span_start + (i + 1) * step)]
            for i, char in enumerate(after)
        ]
        row[3] = words[: covered[0]] + new + words[covered[-1] + 1 :]
        row[2] = row[2][:first] + after + row[2][last:]
        if not row[3]:
            rows.remove(row)
    return _repack(rows)
