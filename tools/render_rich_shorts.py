#!/usr/bin/env python3
"""Generate 3-5 rich shorts from a reviewed manifest or a local automatic plan."""

import argparse
import json
from pathlib import Path

from clipforge.batch import plan, run, transcribe_audio
from clipforge.batch_models import ClipsManifest
from clipforge.kick_media import KickVOD
from clipforge.models import Transcript
from clipforge.storage import digest, fingerprint, write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest", type=Path)
    mode.add_argument("--auto", metavar="KICK_VOD_URL")
    p.add_argument("--count", type=int, choices=[3, 4, 5], default=3)
    p.add_argument("--clip-seconds", type=float, default=45)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path, default=Path("workspace/cache"))
    p.add_argument("--assets", type=Path, default=Path("assets"))
    p.add_argument("--quality", choices=["720p", "1080p", "best"], default="720p")
    p.add_argument("--whisper-model", default="small")
    p.add_argument("--max-bytes", type=int, default=2 * 1024**3)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--plan-only", action="store_true")
    a = p.parse_args()
    try:
        if a.manifest:
            manifest = ClipsManifest.model_validate_json(a.manifest.read_text())
        else:
            # Stage A writes only private audio/transcript caches, never Git files.
            identity = fingerprint({"url": a.auto})
            audio = a.cache / "audio" / (identity + ".m4a")
            receipt = audio.with_suffix(".json")
            if audio.exists():
                info = json.loads(receipt.read_text())
                if digest(audio) != info["sha256"]:
                    raise ValueError("audio cache checksum mismatch")
            else:
                vod = KickVOD(a.auto, a.cache / "segments", a.max_bytes)
                try:
                    info = vod.retrieve(audio, audio_only=True)
                    write_json(receipt, info)
                finally:
                    vod.close()
            transcript_key = fingerprint(
                {"audio": info["sha256"], "model": a.whisper_model, "language": "ja"}
            )
            transcript_path = a.cache / "transcripts" / (transcript_key + ".json")
            if transcript_path.exists():
                transcript = Transcript.model_validate_json(transcript_path.read_text())
            else:
                transcript = transcribe_audio(audio, a.whisper_model)
                write_json(transcript_path, transcript.model_dump())
            manifest = plan(
                a.auto,
                info["source_duration"],
                transcript,
                audio,
                a.count,
                a.clip_seconds,
            )
        if a.plan_only:
            if a.output.exists():
                raise ValueError("plan destination exists")
            write_json(a.output, manifest.model_dump())
            print(json.dumps({"status": "planned", "clips": len(manifest.clips)}))
        else:
            result = run(
                manifest,
                a.output,
                a.cache,
                a.assets,
                a.quality,
                a.whisper_model,
                a.resume,
                a.max_bytes,
            )
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "clips": len(result["clips"]),
                        "output": str(a.output),
                    }
                )
            )
        return 0
    except Exception as e:
        # Paths, provider bodies, playback URLs and transcripts stay out of logs.
        print(json.dumps({"status": "batch_failed", "error_type": type(e).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
