#!/usr/bin/env python3
"""Render a private, reviewed pair of Shorts and topic manifests on a CPU worker."""

import argparse
import json
from pathlib import Path

from clipforge.batch_models import ProductionPlan
from clipforge.batch_workflow import reviewed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path, default=Path("workspace/cache"))
    p.add_argument("--assets", type=Path, default=Path("assets"))
    p.add_argument("--quality", choices=["720p", "1080p", "best"], default="720p")
    p.add_argument("--whisper-model", default="small")
    p.add_argument("--max-bytes", type=int, default=2 * 1024**3)
    p.add_argument("--resume", action="store_true")
    a = p.parse_args()
    try:
        if a.plan.stat().st_size > 8 * 1024**2:
            raise ValueError("plan exceeds size limit")
        plan = ProductionPlan.model_validate_json(a.plan.read_text())
        result = reviewed(
            plan,
            a.output,
            a.cache,
            a.assets,
            a.quality,
            a.whisper_model,
            a.resume,
            a.max_bytes,
        )
        print(json.dumps(result))
        return 0
    except Exception as e:
        print(
            json.dumps(
                {
                    "status": "batch_failed",
                    "error_type": type(e).__name__,
                    "error_code": getattr(e, "code", None),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
