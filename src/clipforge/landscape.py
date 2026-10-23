"""Offline acceptance runner for 3–5 manually selected landscape clips."""

import argparse
from importlib.metadata import version
import json
import math
from pathlib import Path
import subprocess
import sys

from . import fonts, media, pipeline, signals
from .models import SelectedClip, Settings, validate_picks
from .quality import READING_CPS_THRESHOLDS
from .storage import PipelineError, digest, fingerprint, within, write_json


METRICS = {
    "caption_count",
    "caption_max_cps",
    "caption_median_cps",
    "captions_over_limit",
    "captions_under_1s",
    "captions_over_4s",
    "word_splits",
    "orphan_particles",
    "reading_limit_cps",
    "reading_target_cps",
    "horizontal_line_chars",
    "line_max_fullwidth_equivalents",
    "lines_over_horizontal_limit",
    "line_overflows",
    "integrated_lufs",
    "true_peak_dbtp",
    "duration",
    "width",
    "height",
}


def safe_quality_summary(report):
    """Sharing must not copy warning/failure examples, paths, or transcript fragments."""
    metrics = {}
    for name in METRICS:
        value = report.get("metrics", {}).get(name)
        if value is None or (type(value) in (int, float) and math.isfinite(value)):
            metrics[name] = value
    counts = report.get("metrics", {}).get("captions_over_cps", {})
    metrics["captions_over_cps"] = {
        str(cps): counts[str(cps)]
        for cps in READING_CPS_THRESHOLDS
        if type(counts.get(str(cps))) is int and counts[str(cps)] >= 0
    }
    return {
        "passed": report.get("passed") is True,
        "failure_count": len(report.get("failures", [])),
        "warning_count": len(report.get("warnings", [])),
        "metrics": metrics,
    }


def validate_selection(value, settings, duration):
    clips = [SelectedClip.model_validate(row) for row in value["clips"]]
    if not 2 <= len(clips) <= 5 or len(clips) != settings.clips:
        raise PipelineError(
            "landscape delivery needs exactly the requested 2 to 5 clips"
        )
    validate_picks(clips, duration, settings)
    if any(not clip.title.strip() for clip in clips):
        raise PipelineError("each landscape clip needs a topic title")
    ordered = sorted(clips, key=lambda clip: clip.start)
    if any(left.end > right.start for left, right in zip(ordered, ordered[1:])):
        raise PipelineError("landscape selections must not overlap")
    return clips


def preflight(source, selection_file, settings):
    """Inspect local metadata and cache without starting ASR, preview, or render."""
    if (settings.aspect, settings.height, settings.quality_gate) != (
        "16:9",
        1080,
        "strict",
    ):
        raise PipelineError("landscape delivery requires 16:9, 1080 and strict quality")
    if settings.image_search or settings.table_query or settings.chat_url:
        raise PipelineError("offline landscape delivery must disable network lookups")
    if (
        not source.is_file()
        or not 0 < source.stat().st_size <= settings.max_input_bytes
    ):
        raise PipelineError("a local input within the size limit is required")
    info = media.probe(source)
    if not info["audio"] or info["duration"] > settings.max_source_seconds:
        raise PipelineError(
            "landscape input needs speech audio within the duration limit"
        )
    clips = validate_selection(
        json.loads(selection_file.read_text()), settings, info["duration"]
    )
    cached = pipeline.saved_transcript(source, info, settings)
    if settings.require_transcript_cache and cached is None:
        raise PipelineError(
            "a matching valid transcript cache is required; no ASR was started"
        )
    return clips, cached is not None


def code_identity(settings=None):
    root = Path(__file__).parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = None
    result = {
        "git_head": sha,
        "implementation_sha256": fingerprint(
            {p.name: digest(p) for p in root.glob("*.py")}
        ),
        "python": ".".join(map(str, sys.version_info[:3])),
        "packages": {
            name: version(name)
            for name in ("clipforge", "faster-whisper", "pysubs2", "fugashi", "budoux")
        },
    }
    if settings is not None:
        result.update(
            settings_sha256=fingerprint(settings.model_dump()),
            font_sha256=digest(fonts.resolve(settings.font)),
            asr_model=settings.whisper_model,
            asr_revision=signals.model_revision(settings),
            asr_compute="cpu/int8",
        )
        result["ffmpeg"] = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.splitlines()[0]
    return result


def summarize_delivery(manifest, output, expected):
    """Verify retained receipts before reporting numeric delivery results."""
    receipt = manifest.get("selection_receipt", {})
    if (
        manifest.get("status") != "complete"
        or receipt.get("provider") != "manual"
        or receipt.get("generation_requests") != 0
    ):
        raise PipelineError(
            "completed manual selection with zero generation requests is required"
        )
    rows = manifest.get("rendered", [])
    if len(rows) != expected or not 2 <= expected <= 5:
        raise PipelineError("rendered landscape clip count does not match the plan")
    summaries = []
    for index, row in enumerate(rows, 1):
        directory = within(output, output / row["directory"])
        if not pipeline.valid_receipt(directory, row, row.get("key")):
            raise PipelineError("landscape output receipt or files changed")
        info = row["media"]
        if (info["width"], info["height"], info.get("audio_sample_rate")) != (
            1920,
            1080,
            media.AUDIO_RATE,
        ):
            raise PipelineError("landscape output dimensions or audio rate differ")
        report = row["quality"]
        stored_report = json.loads((directory / "quality.json").read_text())
        if (
            report != stored_report
            or not report.get("passed")
            or report.get("failures")
        ):
            raise PipelineError("landscape quality gate did not pass")
        summaries.append({"clip": index, **safe_quality_summary(report)})
    return {
        "status": "complete",
        "rendered_clips": len(rows),
        "clips": summaries,
        "model_generation_requests": 0,
        "human_review_performed": False,
        "topic_content_review": "required",
        "timings": manifest.get("timings", {}),
        "transcript_cache": {
            k: manifest.get("transcript_cache", {}).get(k)
            for k in ("enabled", "key", "reused", "source")
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--selection-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("config.landscape.example.json")
    )
    parser.add_argument("--transcript-cache", type=str)
    parser.add_argument("--clips", type=int, choices=(3, 4, 5))
    parser.add_argument(
        "--allow-asr", action="store_true", help="explicitly permit ASR on a cache miss"
    )
    parser.add_argument(
        "--execute", action="store_true", help="run rendering on an adequate CPU worker"
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text())
        config.update(
            gemini_model="manual-no-api", require_transcript_cache=not args.allow_asr
        )
        if args.transcript_cache is not None:
            config["transcript_cache"] = args.transcript_cache
        if args.clips is not None:
            config["clips"] = args.clips
        settings = Settings.model_validate(config)
        clips, cache_available = preflight(args.input, args.selection_file, settings)
        plan = {
            "status": "prepared",
            "planned_clips": len(clips),
            "width": 1920,
            "height": 1080,
            "transcript_cache_available": cache_available,
            "asr_on_miss_allowed": args.allow_asr,
            "model_generation_requests": 0,
            "topic_content_review": "required",
            "human_review_performed": False,
        }
        if not args.execute:
            print(json.dumps(plan))
            return 0
        identity = code_identity(settings)
        result = pipeline.run(
            args.input,
            args.output,
            settings,
            resume=args.resume,
            selection_file=args.selection_file,
        )
        summary = summarize_delivery(result, args.output, len(clips))
        summary.update(
            reproducibility=identity, selection_sha256=digest(args.selection_file)
        )
        write_json(args.output / "e2e-summary.json", summary)
        print(
            json.dumps(
                {
                    "status": "complete",
                    "rendered_clips": len(clips),
                    "model_generation_requests": 0,
                    "human_review_performed": False,
                }
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, PipelineError):
        # Private quality errors can contain speech examples. Never echo their bodies here.
        print(
            "Landscape verification failed; check local inputs, settings and private run records.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
