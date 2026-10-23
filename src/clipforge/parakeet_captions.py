"""Turn Parakeet character timestamps for reviewed clips into packed captions.

Parakeet returns one segment for a whole clip, so captions are cut at pauses and an
overlong run is cut again at its widest pause. Standard library only.
"""

import argparse
import json
from pathlib import Path

from .caption_pack import pack_captions

PAUSE_SECONDS = 0.35
LONGEST_CHARS = 30
# A zero-length character is given this much time, never past the next character.
MIN_CHAR_SECONDS = 0.08


def _text(unit):
    value = unit.get("char")
    return "".join(value) if isinstance(value, list) else (value or "")


def _runs(chars):
    groups, current = [], []
    for char in chars:
        if current and char[1] - current[-1][2] >= PAUSE_SECONDS:
            groups.append(current)
            current = []
        current.append(char)
    if current:
        groups.append(current)
    runs = []
    while groups:
        group = groups.pop(0)
        if len(group) <= LONGEST_CHARS:
            runs.append(group)
            continue
        cut = max(range(4, len(group) - 4), key=lambda i: group[i][1] - group[i - 1][2])
        groups[:0] = [group[:cut], group[cut:]]
    return runs


def segments(timestamp, clip_start, clip_end):
    """Transcript-shaped segments in source seconds for one clip's Parakeet timestamps."""
    chars = [
        (_text(c), float(c["start"]), float(c["end"]))
        for c in timestamp.get("char", [])
        if _text(c).strip()
    ]
    out = []
    for members in _runs(chars):
        words = []
        for index, (text, start, end) in enumerate(members):
            following = members[index + 1][1] if index + 1 < len(members) else end + 0.3
            a = clip_start + start
            b = clip_start + max(end, min(following, start + MIN_CHAR_SECONDS))
            if words and a < words[-1]["end"]:
                a = words[-1]["end"]
            b = max(b, a + 0.01)
            if b > clip_end:
                break
            words.append({"text": text, "start": round(a, 2), "end": round(b, 2)})
        words = [w for w in words if w["end"] > w["start"]]
        if words:
            out.append(
                {
                    "start": words[0]["start"],
                    "end": words[-1]["end"],
                    "text": "".join(w["text"] for w in words),
                    "words": words,
                }
            )
    return out


def captions(review, results):
    """Packed captions for each reviewed clip, in review order, from parakeet-cN.json."""
    rows = []
    for index, clip in enumerate(review["clips"], 1):
        data = json.loads((Path(results) / f"parakeet-c{index}.json").read_text())
        packed = pack_captions(segments(data["timestamp"], clip["start"], clip["end"]))
        rows.append(
            {
                "candidate_id": clip["candidate_id"],
                "start": clip["start"],
                "end": clip["end"],
                "captions": packed,
            }
        )
    return {"analysis_key": review["analysis_key"], "clips": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    value = captions(json.loads(args.review.read_text()), args.results)
    args.output.write_text(json.dumps(value, ensure_ascii=False))
    print(f"captions for {len(value['clips'])} clips", flush=True)


if __name__ == "__main__":
    main()
