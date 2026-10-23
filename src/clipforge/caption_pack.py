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
