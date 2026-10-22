"""Check local ASS/SRT pairs without downloading or rendering media."""

import argparse
import json
from pathlib import Path

import pysubs2


def check(directory, max_warnings=0):
    directory = Path(directory)
    ass = pysubs2.load(str(directory / "captions.ass"), format_="ass")
    srt = pysubs2.load(str(directory / "captions.srt"), format_="srt")
    events = [event for event in ass if event.style == "Default"]
    if not events:
        raise ValueError("no caption events")
    if [(e.start, e.end, e.plaintext) for e in events] != [
        (e.start, e.end, e.plaintext) for e in srt
    ]:
        raise ValueError("ASS/SRT text or timing differs")
    previous = 0
    for event in events:
        if not previous <= event.start < event.end or event.end - event.start > 4000:
            raise ValueError("caption timing exceeds bounds or overlaps")
        if not event.plaintext.strip() or len(event.plaintext.splitlines()) > 2:
            raise ValueError("caption is empty or exceeds two lines")
        previous = event.end
    review = json.loads((directory / "caption_review.json").read_text())
    warnings = review["warnings"]
    if not isinstance(warnings, list) or len(warnings) > max_warnings:
        raise ValueError("caption warning limit exceeded")
    if review["events"] != len(events) or review["missing_glyphs"]:
        raise ValueError("caption review count or glyph check failed")
    return {"events": len(events), "warnings": len(warnings)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--max-warnings", type=int, default=0)
    args = parser.parse_args(argv)
    if args.max_warnings < 0:
        parser.error("--max-warnings must be nonnegative")
    try:
        result = check(args.directory, args.max_warnings)
    except (ValueError, KeyError, OSError):
        print("Caption quality check failed; caption text is not logged.")
        return 1
    print(json.dumps({"caption_check": "passed", **result}))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wiring
    raise SystemExit(main())
