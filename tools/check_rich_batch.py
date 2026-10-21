#!/usr/bin/env python3
"""Verify a completed batch's media, captions, hashes and original timestamps."""

import argparse
import json
from pathlib import Path

from clipforge import media
from clipforge.batch_models import ClipsManifest
from clipforge.storage import digest, process


def check(root):
    manifest = ClipsManifest.model_validate_json(
        (root / "clips_manifest.json").read_text()
    )
    result = json.loads((root / "batch_result.json").read_text())
    assert result["status"] == "complete" and len(result["clips"]) == len(
        manifest.clips
    )
    rows = []
    for clip in manifest.clips:
        directory = root / clip.clip_id
        receipt = json.loads((directory / "render.json").read_text())
        for name, sha in receipt["files_sha256"].items():
            assert digest(directory / name) == sha
        info = media.probe(directory / "video.mp4")
        assert abs(info["duration"] - (clip.end_time - clip.start_time)) <= 0.3
        assert info["audio"] and info["height"] in (720, 1080)
        assert info["width"] == round(info["height"] * 9 / 16 / 2) * 2
        import pysubs2

        subs = pysubs2.SSAFile.load(str(directory / "captions.ass"))
        assert any(e.style == "Headline" for e in subs)
        assert all(
            0 <= e.start < e.end <= round(info["duration"] * 1000) + 100 for e in subs
        )
        process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                *media.PROTOCOLS,
                "-i",
                directory / "video.mp4",
                "-f",
                "null",
                "-",
            ],
            timeout=600,
        )
        rows.append(
            {
                "clip_id": clip.clip_id,
                "seconds": info["duration"],
                "source": [clip.start_time, clip.end_time],
                "subtitle_events": len(clip.subtitles),
                "assets": list(receipt["assets_sha256"]),
                "sha256": receipt["output_sha256"],
            }
        )
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    a = p.parse_args()
    print(
        json.dumps(
            {"status": "verified", "clips": check(a.directory)}, ensure_ascii=False
        )
    )
