#!/usr/bin/env python3
"""Re-render one affected clip on a private worker from verified local inputs."""

import argparse
import json
from pathlib import Path

import pysubs2

from clipforge.batch_models import ClipsManifest
from clipforge import media
from clipforge.quality import inspect_video
from clipforge.rich_render import render
from clipforge.storage import PipelineError, digest, process, write_json


def caption_rows(path):
    return [
        (e.start, e.end, e.plaintext)
        for e in pysubs2.load(str(path))
        if e.style == "Default"
    ]


def verify(previous, cache, output, assets, kind, clip_id):
    previous, cache, output = map(
        lambda p: Path(p).resolve(), (previous, cache, output)
    )
    manifest = ClipsManifest.model_validate_json(
        (previous / kind / "clips_manifest.json").read_text()
    )
    clip = next(c for c in manifest.clips if c.clip_id == clip_id)
    old_dir = previous / kind / clip_id
    old = json.loads((old_dir / "render.json").read_text())
    if old["clip"] != clip.model_dump() or old["quality"]["status"] != "passed":
        raise PipelineError(
            "previous clip receipt is inconsistent", code="previous_clip_integrity"
        )
    required = {"video.mp4", "captions.ass", "captions.srt", "caption_review.json"}
    if set(old["files_sha256"]) != required or any(
        digest(old_dir / name) != sha for name, sha in old["files_sha256"].items()
    ):
        raise PipelineError(
            "previous clip files changed", code="previous_clip_integrity"
        )
    candidates = [
        p.with_suffix(".mp4")
        for p in (cache / "ranges").glob("*.json")
        if json.loads(p.read_text()).get("sha256") == old["source_sha256"]
    ]
    if not candidates:
        raise PipelineError(
            "verified source is missing from cache", code="source_cache_required"
        )
    source = candidates[0]
    if digest(source) != old["source_sha256"]:
        raise PipelineError("cached source changed", code="source_integrity")
    if old["style"] != manifest.style.model_dump() or old["output_kind"] != kind:
        raise PipelineError(
            "previous style/profile changed", code="previous_clip_integrity"
        )
    for name, sha in old["assets_sha256"].items():
        asset = (Path(assets).resolve() / name).resolve()
        if not asset.is_relative_to(Path(assets).resolve()) or digest(asset) != sha:
            raise PipelineError(
                "caption verification requires unchanged assets", code="asset_integrity"
            )
    # No media retrieval or ASR is performed by this entry point.
    receipt = render(source, output, clip, manifest.style, assets, kind)
    if receipt["assets_sha256"] != old["assets_sha256"]:
        raise PipelineError(
            "caption verification requires unchanged assets", code="asset_integrity"
        )
    before = caption_rows(old_dir / "captions.ass")
    after = caption_rows(output / "captions.ass")
    srt = [
        (e.start, e.end, e.plaintext)
        for e in pysubs2.load(str(output / "captions.srt"))
    ]
    if after != srt or not after:
        raise PipelineError("ASS/SRT differ", code="caption_consistency")

    def text(rows):
        return "".join("".join(row[2].split()) for row in rows)

    if text(before) != text(after):
        raise PipelineError(
            "caption update changed dialogue", code="caption_text_changed"
        )
    if any(
        b - a < 1000 or b - a > 4000 or len(t.splitlines()) > 2 for a, b, t in after
    ) or any(a[1] > b[0] for a, b in zip(after, after[1:])):
        raise PipelineError(
            "caption duration/line constraints failed", code="caption_layout"
        )
    # Caption changes need new video pixels, but should retain the exact approved
    # audio track rather than replacing it with another AAC encoding.
    remuxed = output / ".audio-preserved.mp4"
    process(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            output / "video.mp4",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            old_dir / "video.mp4",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            remuxed,
        ],
        timeout=600,
    )
    remuxed.replace(output / "video.mp4")
    receipt["media"] = media.probe(output / "video.mp4")
    receipt["output_sha256"] = digest(output / "video.mp4")
    receipt["quality"] = inspect_video(
        output / "video.mp4",
        clip.end_time - clip.start_time,
        receipt["media"]["width"],
        receipt["media"]["height"],
    )

    def audio_hash(path):
        return process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                path,
                "-map",
                "0:a:0",
                "-vn",
                "-f",
                "hash",
                "-hash",
                "sha256",
                "-",
            ],
            timeout=600,
        ).stdout.strip()

    if audio_hash(old_dir / "video.mp4") != audio_hash(output / "video.mp4"):
        raise PipelineError(
            "caption update changed decoded audio", code="audio_changed"
        )
    receipt.update(
        files_sha256={name: digest(output / name) for name in required},
        previous_output_sha256=old["output_sha256"],
    )
    write_json(output / "render.json", receipt)
    report = {
        "status": "verified",
        "output_kind": kind,
        "clip_id": clip_id,
        "caption_events": len(after),
        "caption_warnings": len(receipt["caption_review"]["warnings"]),
        "last_cue_seconds": (after[-1][1] - after[-1][0]) / 1000,
        "dialogue_preserved": True,
        "decoded_audio_unchanged": True,
        "source_sha256": old["source_sha256"],
        "quality": receipt["quality"],
        "human_review_performed": False,
    }
    write_json(output / "update_result.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--assets", type=Path, default=Path("assets"))
    p.add_argument("--output-kind", choices=["shorts", "topics"], required=True)
    p.add_argument("--clip-id", required=True)
    a = p.parse_args()
    try:
        print(
            json.dumps(
                verify(
                    a.previous, a.cache, a.output, a.assets, a.output_kind, a.clip_id
                )
            )
        )
        return 0
    except Exception as e:
        print(
            json.dumps(
                {
                    "status": "caption_update_failed",
                    "error_type": type(e).__name__,
                    "error_code": getattr(e, "code", None),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
