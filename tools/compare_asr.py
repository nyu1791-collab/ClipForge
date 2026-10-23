#!/usr/bin/env python3
"""Compare two private analysis outputs of one source, such as one-part and two-part ASR.

Prints numbers only, never transcript text, so the result can be shared in a report.
Both directories are submit_job outputs (topics.json, request-receipt.json, transcripts/).
"""

import argparse
import bisect
import difflib
import json
from pathlib import Path
import statistics
import sys

THRESHOLDS = (0.5, 2.0, 10.0, 30.0)
WINDOW = 60.0


def load(directory):
    directory = Path(directory)
    transcripts = sorted((directory / "transcripts").glob("*.json"))
    if len(transcripts) != 1:
        raise SystemExit(f"{directory}: expected exactly one transcript")
    topics = json.loads((directory / "topics.json").read_text())
    return {
        "receipt": json.loads((directory / "request-receipt.json").read_text()),
        "asr": json.loads((directory / "asr-receipt.json").read_text()),
        "segments": json.loads(transcripts[0].read_text())["segments"],
        "topics": topics["topics"] if isinstance(topics, dict) else topics,
    }


def text(segments, start, end):
    return "".join(s["text"] for s in segments if start <= s["start"] < end)


def similarity(a, b):
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def overlapping_words(segments, start, end):
    """Words that begin before the previous word ends: a duplicate across the cut shows here."""
    words = sorted(
        (w for s in segments for w in s.get("words", []) if start <= w["start"] < end),
        key=lambda w: w["start"],
    )
    return sum(b["start"] < a["end"] - 0.05 for a, b in zip(words, words[1:]))


def _split(receipt):
    """The cut of a two-part analysis, kept at the top level or under its origin."""
    return receipt.get("split") or (receipt.get("origin") or {}).get("split")


def first_difference(a, b):
    """Earliest start where the two segment lists stop agreeing, or None if identical."""
    for x, y in zip(a, b):
        if (x["start"], x["text"]) != (y["start"], y["text"]):
            return min(x["start"], y["start"])
    longer = a[len(b) :] or b[len(a) :]
    return longer[0]["start"] if longer else None


def drift(base, other):
    """Distance from each baseline topic boundary to the nearest boundary of the other."""
    starts = [t["start"] for t in other[1:]]
    result = []
    for topic in base[1:]:
        i = bisect.bisect_left(starts, topic["start"])
        near = [starts[j] for j in (i - 1, i) if 0 <= j < len(starts)]
        result.append((topic["start"], min(abs(topic["start"] - v) for v in near)))
    return result


def boundary_stats(rows):
    values = [d for _, d in rows]
    if not values:
        return {"boundaries": 0}
    return {
        "boundaries": len(values),
        **{f"within_{t:g}s": sum(v <= t for v in values) for t in THRESHOLDS},
        "median_s": round(statistics.median(values), 3),
        "max_s": round(max(values), 3),
    }


def compare(base, other):
    split = (_split(other["asr"]) or _split(base["asr"]) or {}).get("silence")
    cut = sum(split) / 2 if split else None
    end = max(base["segments"][-1]["end"], other["segments"][-1]["end"]) + 1
    halves = [("whole", 0.0, end)]
    if cut is not None:
        halves += [("before_cut", 0.0, cut), ("after_cut", cut, end)]
    rows = drift(base["topics"], other["topics"])
    report = {
        "source_ref": [
            base["receipt"].get("source_ref"),
            other["receipt"].get("source_ref"),
        ],
        "same_code": base["receipt"].get("source_ref")
        == other["receipt"].get("source_ref"),
        "asr_source": [base["asr"].get("source"), other["asr"].get("source")],
        "cut_s": None if cut is None else round(cut, 3),
        "topics": [len(base["topics"]), len(other["topics"])],
        "segments": [len(base["segments"]), len(other["segments"])],
        "first_differing_segment_s": first_difference(
            base["segments"], other["segments"]
        ),
    }
    for name, start, stop in halves:
        a, b = text(base["segments"], start, stop), text(other["segments"], start, stop)
        report[name] = {
            "characters": [len(a), len(b)],
            "text_similarity": similarity(a, b),
            **boundary_stats([r for r in rows if start <= r[0] < stop]),
        }
    if cut is not None:
        start, stop = cut - WINDOW, cut + WINDOW
        report["around_cut"] = {
            "window_s": [round(start, 3), round(stop, 3)],
            "text_similarity": similarity(
                text(base["segments"], start, stop),
                text(other["segments"], start, stop),
            ),
            "overlapping_words": [
                overlapping_words(base["segments"], start, stop),
                overlapping_words(other["segments"], start, stop),
            ],
            "topic_spanning_cut": [
                [[t["start"], t["end"]] for t in run if t["start"] < cut < t["end"]]
                for run in (base["topics"], other["topics"])
            ],
        }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("other", type=Path)
    args = parser.parse_args(argv)
    report = compare(load(args.baseline), load(args.other))
    print(json.dumps(report, indent=2))
    if not report["same_code"]:
        print(
            "warning: the outputs come from different code; a difference may not be the split's",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
