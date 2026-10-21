"""ClipForge command-line entry point. No inference occurs at import time."""

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(
        description="CPU video clipping and visual Gemini selection"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["run", "cut"]:
        p = sub.add_parser(name)
        p.add_argument("input", type=Path)
        p.add_argument("--output", type=Path, required=True)
        p.add_argument("--config", type=Path)
        p.add_argument("--resume", action="store_true")
        p.add_argument("--aspect", choices=["9:16", "16:9", "1:1"])
        p.add_argument("--framing", choices=["fit", "center-crop"])
        p.add_argument("--height", type=int, choices=[720, 1080, 1920])
        if name == "run":
            p.add_argument("--gemini-model")
            p.add_argument("--clips", type=int)
            p.add_argument("--candidates", type=int)
            p.add_argument("--whisper-model")
            p.add_argument("--language")
            p.add_argument("--chat-url")
            p.add_argument("--chat-offset-seconds", type=float)
            p.add_argument("--chat-file", type=Path)
            p.add_argument("--selection-file", type=Path)
        else:
            p.add_argument("--start", type=float, required=True)
            p.add_argument("--end", type=float, required=True)
            p.add_argument("--transcript", type=Path)
    batch = sub.add_parser("batch", help="several videos, each in its own folder")
    batch.add_argument("inputs", type=Path, nargs="+")
    batch.add_argument("--output-root", type=Path, required=True)
    batch.add_argument("--config", type=Path)
    batch.add_argument("--gemini-model")
    batch.add_argument("--concurrency", type=int, help="videos processed at once")
    batch.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        from .models import Settings
        from .pipeline import cut, default_concurrency, run, run_batch

        config = json.loads(args.config.read_text()) if args.config else {}
        for key in [
            "aspect",
            "framing",
            "height",
            "gemini_model",
            "clips",
            "candidates",
            "whisper_model",
            "language",
            "chat_url",
            "chat_offset_seconds",
        ]:
            value = getattr(args, key, None)
            if value is not None:
                config[key] = value
        if args.command == "cut":
            config.setdefault("gemini_model", "manual-no-api")
        if not config.get("gemini_model"):
            raise ValueError(
                "Specify an explicit --gemini-model or config.gemini_model"
            )
        settings = Settings.model_validate(config)
        if args.command == "batch":
            concurrency = args.concurrency or default_concurrency(settings)
            results = run_batch(
                args.inputs, args.output_root, settings, concurrency, args.resume
            )
            failed = sum(item["status"] == "failed" for item in results)
            print(
                json.dumps(
                    {
                        "videos": len(results),
                        "complete": len(results) - failed,
                        "failed": failed,
                        "human_review_performed": False,
                    },
                    ensure_ascii=False,
                )
            )
            return 1 if failed else 0
        if args.command == "run":
            result = run(
                args.input,
                args.output,
                settings,
                args.resume,
                args.chat_file,
                args.selection_file,
            )
        else:
            result = cut(
                args.input,
                args.output,
                args.start,
                args.end,
                settings,
                args.transcript,
                args.resume,
            )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "rendered_clips": len(result["rendered"]),
                    "human_review_performed": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except KeyboardInterrupt:
        print("Interrupted; completed stages can be resumed.", file=sys.stderr)
        return 130
    except Exception as error:
        # Do not print SDK bodies, transcript text, URLs or credential-bearing errors.
        from .storage import PipelineError

        detail = (
            str(error) if isinstance(error, PipelineError) else type(error).__name__
        )
        print("ClipForge failed: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
