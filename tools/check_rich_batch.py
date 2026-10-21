#!/usr/bin/env python3
"""Verify a completed batch's media, captions, hashes and original timestamps."""

import argparse
import json
from pathlib import Path

from clipforge import media
from clipforge.batch_models import ClipsManifest
from clipforge.storage import digest, process


def check(root):
    root = Path(root)
    expected = None
    if (root / "production_result.json").exists():
        production = json.loads((root / "production_result.json").read_text())
        assert production["status"] == "complete"
        expected = {row["output_kind"]: row["clips"] for row in production["outputs"]}
        assert len(expected) == len(production["outputs"])
        assert expected and set(expected) <= {"shorts", "topics"}
        if not (root / "clips_manifest.json").exists():
            reports = []
            for kind, count in expected.items():
                clips = check(root / kind)
                assert len(clips) == count
                reports.append(dict(output_kind=kind, clips=clips))
            return reports
    manifest = ClipsManifest.model_validate_json(
        (root / "clips_manifest.json").read_text()
    )
    result = json.loads((root / "batch_result.json").read_text())
    if expected is not None:
        assert expected == {manifest.output_kind: len(manifest.clips)}
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
        ratio = 16 / 9 if manifest.output_kind == "topics" else 9 / 16
        assert info["width"] == round(info["height"] * ratio / 2) * 2
        import pysubs2

        subs = pysubs2.SSAFile.load(str(directory / "captions.ass"))
        assert any(e.style == "Headline" for e in subs)
        assert all(
            0 <= e.start < e.end <= round(info["duration"] * 1000) + 100 for e in subs
        )
        captions = [e for e in subs if e.style == "Default"]
        assert all(len(e.plaintext.splitlines()) <= 2 for e in captions)
        assert all(a.end <= b.start for a, b in zip(captions, captions[1:]))
        if "caption_review" in receipt:
            assert receipt["quality"]["status"] == "passed"
            if receipt["quality"].get("gate_version", 1) >= 2:
                assert info["audio_sample_rate"] == 48000
            assert len(captions) == receipt["caption_events"]
            srt = pysubs2.SSAFile.load(str(directory / "captions.srt"))
            assert [(e.start, e.end, e.plaintext) for e in captions] == [
                (e.start, e.end, e.plaintext) for e in srt
            ]
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
                "asr_segments": len(clip.subtitles),
                "caption_events": len(captions),
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
