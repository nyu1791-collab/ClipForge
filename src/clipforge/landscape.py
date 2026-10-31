"""Reviewed landscape clip selection, code identity and delivery summary."""

from importlib.metadata import version
import math
from pathlib import Path
import subprocess
import sys

from . import fonts, pipeline
from .models import SelectedClip, validate_picks
from .storage import PipelineError, digest, fingerprint, within


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

READING_CPS_THRESHOLDS = (4, 7, 10)


def safe_quality_summary(report):
    """Sharing must not copy warning/failure examples, paths, or transcript fragments."""
    if report.get("status") == "not_run":
        return {"status": "not_run", "passed": None, "verification_scope": "none"}
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
        "verification_scope": report.get("verification_scope", "unknown"),
        "full_decode_performed": report.get("full_decode_performed", False),
        "loudness_measured": report.get("loudness_measured", False),
        "verify_seconds": report.get("verify_seconds"),
        "failure_count": len(report.get("failures", [])),
        "warning_count": len(report.get("warnings", [])),
        "metrics": metrics,
    }


def validate_selection(value, settings, duration, *, advisory_targets=False):
    clips = [SelectedClip.model_validate(row) for row in value["clips"]]
    # Editorial targets never gate an otherwise renderable delivery.
    validate_picks(clips, duration, settings)
    clips = [
        clip
        if clip.title.strip()
        else clip.model_copy(update={"title": f"話題 {index}"})
        for index, clip in enumerate(clips, 1)
    ]
    return clips


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
            for name in ("clipforge", "pysubs2", "fugashi", "budoux")
        },
    }
    if settings is not None:
        result.update(
            settings_sha256=fingerprint(settings.model_dump()),
            font_sha256=digest(fonts.resolve(settings.font)),
        )
        result["ffmpeg"] = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.splitlines()[0]
    return result


def summarize_delivery(manifest, output, expected, height=1080):
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
    summaries = []
    for index, row in enumerate(rows, 1):
        directory = within(output, output / row["directory"])
        if not pipeline.valid_receipt(directory, row, row.get("key")):
            raise PipelineError("landscape output receipt or files changed")
        report = row.get("quality", {"status": "not_run", "passed": None})
        summaries.append({"clip": index, **safe_quality_summary(report)})
    return {
        "status": "complete",
        "rendered_clips": len(rows),
        "target_clips": expected,
        "target_count_met": len(rows) == expected,
        "clips": summaries,
        "model_generation_requests": 0,
        "human_review_performed": False,
        "topic_content_review": "not_required",
        "timings": manifest.get("timings", {}),
        "transcript_cache": {
            k: manifest.get("transcript_cache", {}).get(k)
            for k in ("enabled", "key", "reused", "source")
        },
    }
