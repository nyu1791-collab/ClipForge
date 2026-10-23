#!/usr/bin/env python3
"""Attach separately recognised captions to the matching clips of a review file."""

import json
import os
from pathlib import Path
import sys


def attach(review, captions):
    if captions.get("analysis_key") != review.get("analysis_key"):
        raise SystemExit("captions belong to a different analysis")
    rows = captions.get("clips") or []
    for clip in review["clips"]:
        match = [
            row
            for row in rows
            if row.get("candidate_id") == clip.get("candidate_id")
            and abs(row.get("start", -1) - clip.get("start", -2)) < 0.005
            and abs(row.get("end", -1) - clip.get("end", -2)) < 0.005
        ]
        if len(match) != 1 or not isinstance(match[0].get("captions"), str):
            raise SystemExit(f"no captions for {clip.get('candidate_id')}")
        clip["captions"] = match[0]["captions"]
    return review


def main(review_path, captions_path):
    review = attach(
        json.loads(Path(review_path).read_text()),
        json.loads(Path(captions_path).read_text()),
    )
    Path(review_path).write_text(json.dumps(review, ensure_ascii=False))
    os.chmod(review_path, 0o600)
    print(f"captions attached to {len(review['clips'])} clips", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:3])
