"""OpenCode and CI share this offline verification entry point. No inference."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipforge.batch_models import ClipsManifest, ProductionPlan  # noqa: E402
from tools.opencode_harness import validate  # noqa: E402


def check_contracts(root=ROOT):
    validate(root)
    for name, model in (
        ("clips_manifest", ClipsManifest),
        ("production_plan", ProductionPlan),
    ):
        saved = json.loads((root / "schemas" / (name + ".schema.json")).read_text())
        if saved != model.model_json_schema():
            raise ValueError("published schema differs from runtime model")


def steps():
    python = sys.executable
    targets = ["src", "tests", "tools", "harness"]
    return [
        (
            "lint",
            [python, "-m", "ruff", "check", "--config", "pyproject.toml", *targets],
        ),
        (
            "format",
            [
                python,
                "-m",
                "ruff",
                "format",
                "--check",
                "--config",
                "pyproject.toml",
                *targets,
            ],
        ),
        (
            "offline regression tests",
            [
                python,
                "-m",
                "coverage",
                "run",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
        ),
        ("coverage report (entire application)", [python, "-m", "coverage", "report"]),
        (
            "harness branch coverage >=95%",
            [
                python,
                "-m",
                "coverage",
                "report",
                "--include=*/harness/*",
                "--fail-under=95",
            ],
        ),
        ("diff whitespace", ["git", "diff", "--check"]),
        ("staged diff whitespace", ["git", "diff", "--cached", "--check"]),
    ]


def main():
    try:
        check_contracts()
    except (ValueError, OSError, KeyError):
        print("Harness configuration or published schema check failed.", flush=True)
        return 1
    for description, command in steps():
        print(f"[harness] {description}", flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, check=False, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            print(f"[harness] failed to finish: {description}", flush=True)
            return 1
        if result.returncode:
            print(f"[harness] failed: {description}", flush=True)
            return 1
    print("[harness] all offline checks passed; no model or video processing run.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wiring
    raise SystemExit(main())
