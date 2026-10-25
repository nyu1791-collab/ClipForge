#!/usr/bin/env python3
"""Resolve a supplied video URL, let Gemini plan it, and render without a handoff gate."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from clipforge.media_sources import canonical
from clipforge.release_transfer import download, read_transfer


def gh(*args, body=None):
    p = subprocess.run(["gh", *args], input=body, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(
            "GitHub operation failed; inspect the saved request, no resubmission"
        )
    return p.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--source-ref", required=True)
    ap.add_argument(
        "--repository",
        choices=["nyu1791-collab/ClipForge", "nyu1791-collab/ai-model-workers"],
        default="nyu1791-collab/ClipForge",
    )
    ap.add_argument("--analysis-run")
    ap.add_argument("--analysis-request")
    ap.add_argument("--clips", type=int, choices=range(5, 11))
    ap.add_argument("--config-file", default="config.landscape-long.example.json")
    ap.add_argument(
        "--deliver-drive",
        action="store_true",
        help="Upload once to the explicitly configured target Drive after rendering",
    )
    ap.add_argument("--timeout", type=int, default=7200)
    args = ap.parse_args()
    if not re.fullmatch("[a-f0-9]{40}", args.source_ref):
        ap.error("source-ref must be an exact commit SHA")
    if bool(args.analysis_run) != bool(args.analysis_request):
        ap.error("both saved analysis identities are required")
    if args.analysis_run and (
        not re.fullmatch(r"[1-9][0-9]*", args.analysis_run)
        or not re.fullmatch("[a-f0-9]{32}", args.analysis_request)
    ):
        ap.error("invalid saved analysis identity")
    if not re.fullmatch(r"config\.[a-z0-9-]+\.example\.json", args.config_file):
        ap.error("config-file must name a committed production config")
    settings = json.loads(
        (Path(__file__).resolve().parents[1] / args.config_file).read_text()
    )
    requested_count = args.clips if args.clips is not None else settings.get("clips", 5)
    if not 5 <= requested_count <= 10 or settings.get("clips") != requested_count:
        ap.error("the committed config must specify the requested 5 to 10 clips")
    args.clips = requested_count
    os.umask(0o077)
    url = canonical(args.url)
    args.output.mkdir(parents=True, exist_ok=True)
    statefile = args.output / "production-state.json"
    if statefile.exists():
        raise RuntimeError(
            "This production already has a saved state; inspect its run before any new submission"
        )
    state = {
        "source_url": url,
        "source_ref": args.source_ref,
        "repository": args.repository,
        "started_at": time.time(),
        "stages": [],
    }
    deadline = time.monotonic() + args.timeout

    def save():
        statefile.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def dispatch(workflow, inputs, delivery=False):
        identity = uuid.uuid4().hex
        if delivery:
            inputs = {
                **inputs,
                "input_tag": "clipforge-drive-" + identity,
                "source_ref": args.source_ref,
            }
            expected_title = "ClipForge Drive upload-release " + inputs["input_tag"]
        else:
            inputs = {
                **inputs,
                "request_id": identity,
                "input_tag": "clipforge-input-" + identity,
                "source_ref": args.source_ref,
                "result_store": "release",
            }
            expected_title = "ClipForge " + identity
        stage = {
            "workflow": workflow,
            "request_id": identity,
            "status": "dispatching_or_unknown",
            "started_at": time.time(),
        }
        state["stages"].append(stage)
        save()
        ref = (
            "main"
            if args.repository.endswith("/ClipForge")
            else "fix/pr18-verified-transfer"
        )
        gh(
            "api",
            "--method",
            "POST",
            f"repos/{args.repository}/actions/workflows/{workflow}/dispatches",
            "--input",
            "-",
            body=json.dumps({"ref": ref, "inputs": inputs}),
        )
        stage["status"] = "dispatched"
        save()
        while time.monotonic() < deadline:
            if "run_id" not in stage:
                runs = json.loads(
                    gh(
                        "run",
                        "list",
                        "--repo",
                        args.repository,
                        "--workflow",
                        workflow,
                        "--limit",
                        "50",
                        "--json",
                        "databaseId,displayTitle,event",
                    )
                )
                found = [
                    r
                    for r in runs
                    if r["displayTitle"] == expected_title
                    and r["event"] == "workflow_dispatch"
                ]
                if len(found) > 1:
                    raise RuntimeError("Duplicate run identity; stopped")
                if found:
                    stage["run_id"] = found[0]["databaseId"]
                    save()
            if "run_id" in stage:
                r = json.loads(
                    gh(
                        "run",
                        "view",
                        str(stage["run_id"]),
                        "--repo",
                        args.repository,
                        "--json",
                        "status,conclusion,url",
                    )
                )
                stage.update(r)
                save()
                print(
                    json.dumps({"workflow": workflow, "run_id": stage["run_id"], **r}),
                    flush=True,
                )
                if r["status"] == "completed":
                    stage["elapsed_sec"] = round(time.time() - stage["started_at"], 3)
                    save()
                    if r["conclusion"] != "success":
                        raise RuntimeError("Stage failed; no retry or model fallback")
                    return str(stage["run_id"]), identity
            time.sleep(15)
        raise RuntimeError(
            "Production timeout; inspect the recorded run, no resubmission"
        )

    try:
        if args.analysis_run:
            run, request = dispatch(
                "clipforge-stages.yml",
                {
                    "audio_stage": "previews",
                    "worker": "cpu",
                    "previous_run_id": args.analysis_run,
                    "previous_request_id": args.analysis_request,
                    "config_file": args.config_file,
                },
            )
            name = "clipforge-result-" + request
        else:
            run, request = dispatch(
                "clipforge.yml",
                {
                    "audio_url": url,
                    "auto_previews": True,
                    "settings_json": json.dumps(settings),
                },
            )
            name = "clipforge-previews-" + request
        metadata = read_transfer(run, name)
        manifest = metadata["stage_manifest"]
        if canonical(manifest["source_url"]) != url:
            raise RuntimeError("Saved analysis belongs to a different source URL")
        selection = manifest["automatic_selection"]
        if len(selection["clips"]) != args.clips:
            raise RuntimeError("Gemini selection count differs; no extra generation")
        (args.output / "gemini-selection.json").write_text(
            json.dumps(selection, ensure_ascii=False, indent=2)
        )
        final_run, final_request = dispatch(
            "clipforge-stages.yml",
            {
                "audio_stage": "render",
                "worker": "colab-auto",
                "previous_run_id": run,
                "previous_request_id": request,
                "review_json": json.dumps(selection, ensure_ascii=False),
                "config_file": args.config_file,
            },
        )
        state["render_run_id"] = final_run
        state["render_request_id"] = final_request
        save()
        download(final_run, "clipforge-result-" + final_request, args.output / "clips")
        state["render_complete"] = True
        save()
        if args.deliver_drive:
            drive_run, _ = dispatch(
                "clipforge-drive-delivery.yml",
                {
                    "mode": "upload-release",
                    "render_run": final_run,
                    "render_request": final_request,
                },
                delivery=True,
            )
            state["drive_run_id"] = drive_run
            state["drive_status"] = "uploaded"
        state.update(
            status="complete", elapsed_sec=round(time.time() - state["started_at"], 3)
        )
        save()
        print(
            json.dumps(
                {
                    "status": "complete",
                    "clips": args.clips,
                    "output": str(args.output / "clips"),
                    "elapsed_sec": state["elapsed_sec"],
                }
            ),
            flush=True,
        )
    except Exception:
        state.update(
            status="delivery_failed_or_unknown"
            if state.get("render_complete")
            else "failed_or_unknown",
            elapsed_sec=round(time.time() - state["started_at"], 3),
        )
        save()
        raise


if __name__ == "__main__":
    main()
