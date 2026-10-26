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

from clipforge.editorial_revision import EDITORIAL_TAG_PATTERN, create_revision
from clipforge.actions_monitor import MonitoringPending, wait_for_stage
from clipforge.media_sources import canonical
from clipforge.release_transfer import download, read_transfer


def gh(*args, body=None):
    p = subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, timeout=120
    )
    if p.returncode:
        raise RuntimeError(
            "GitHub operation failed; inspect the saved request, no resubmission"
        )
    return p.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument(
        "--resume-existing-run",
        type=int,
        help="Observe and collect this saved production run; never dispatch another run",
    )
    ap.add_argument(
        "--source-ref",
        help="Pin this commit; omitted resolves the execution branch once before dispatch",
    )
    ap.add_argument(
        "--repository",
        choices=["nyu1791-collab/ClipForge", "nyu1791-collab/ai-model-workers"],
        default="nyu1791-collab/ClipForge",
    )
    ap.add_argument("--workflow-ref", default="fix/pr18-verified-transfer")
    ap.add_argument("--editorial-release", default="")
    ap.add_argument("--continue-missing", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--analysis-run", "--previous-run", dest="analysis_run")
    ap.add_argument("--analysis-request", "--previous-request", dest="analysis_request")
    ap.add_argument(
        "--revision-file",
        type=Path,
        help="Private explicit edits; requires render-only",
    )
    ap.add_argument(
        "--reuse-render",
        action="store_true",
        help="Reuse completed clips from the previous render pair",
    )
    ap.add_argument("--clips", type=int)
    ap.add_argument("--config-file", default="config.landscape-long.example.json")
    ap.add_argument(
        "--render-profile",
        choices=["preview", "standard", "full-hd"],
        help="360p / 720p / 1080p text canvas; source stays <=360p",
    )
    ap.add_argument(
        "--deliver-drive",
        action="store_true",
        help="Upload once to the explicitly configured target Drive after rendering",
    )
    ap.add_argument("--timeout", type=int, default=7200)
    args = ap.parse_args()
    resumed = None
    if args.resume_existing_run is not None:
        if args.resume_existing_run <= 0:
            ap.error("resume-existing-run requires a positive run ID")
        resumed = json.loads((args.output / "production-state.json").read_text())
        if canonical(resumed["source_url"]) != canonical(args.url):
            ap.error("saved production belongs to another source URL")
        if args.source_ref and args.source_ref != resumed["source_ref"]:
            ap.error("saved production uses another source commit")
        if (
            args.revision_file
            or args.continue_missing
            or args.deliver_drive
            or args.render_profile
        ):
            ap.error(
                "resume-existing-run only observes and collects the saved production"
            )
        args.source_ref = resumed["source_ref"]
        args.repository = resumed["repository"]
        args.config_file = resumed["config_file"]
    if args.render_profile:
        args.config_file = {
            "preview": "config.landscape-long-preview.example.json",
            "standard": "config.landscape-long.example.json",
            "full-hd": "config.landscape-long-fullhd.example.json",
        }[args.render_profile]
    if args.workflow_ref not in {"main", "fix/pr18-verified-transfer"}:
        ap.error("workflow-ref must use a configured public execution branch")
    if args.editorial_release and not re.fullmatch(
        EDITORIAL_TAG_PATTERN, args.editorial_release
    ):
        ap.error("invalid saved editorial release")
    if args.revision_file and not (args.render_only and args.editorial_release):
        ap.error("revision-file requires render-only and the parent editorial-release")
    if "-revision-" in args.editorial_release and not args.render_only:
        ap.error(
            "An editorial revision goes directly to render-only, never back to Gemini"
        )
    if args.reuse_render and not args.render_only:
        ap.error(
            "reuse-render requires render-only and the previous render run/request pair"
        )
    if args.render_only and args.continue_missing:
        ap.error("render-only does not generate missing model responses")
    if args.continue_missing and not args.editorial_release:
        ap.error("continue-missing requires editorial-release")
    if args.render_only and not (
        args.editorial_release and args.analysis_run and args.analysis_request
    ):
        ap.error(
            "render-only requires editorial-release and the saved run/request pair"
        )
    if args.repository != "nyu1791-collab/ClipForge":
        ap.error("new audio-understanding production runs from ClipForge")
    if not args.source_ref:
        args.source_ref = gh(
            "api",
            f"repos/{args.repository}/commits/{args.workflow_ref}",
            "--jq",
            ".sha",
        ).strip()
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
    args.clips = requested_count
    os.umask(0o077)
    url = canonical(args.url)
    args.output.mkdir(parents=True, exist_ok=True)
    statefile = args.output / "production-state.json"
    if statefile.exists() and resumed is None:
        raise RuntimeError(
            "This production already has a saved state; inspect its run before any new submission"
        )
    state = (
        resumed
        if resumed is not None
        else {
            "source_url": url,
            "source_ref": args.source_ref,
            "repository": args.repository,
            "config_file": args.config_file,
            "render_height": settings.get("height"),
            "source_height_limit": 360,
            "target_clips": args.clips,
            "started_at": time.time(),
            "stages": [],
        }
    )
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
            "expected_title": expected_title,
        }
        state["stages"].append(stage)
        save()
        ref = args.workflow_ref
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
        return wait_for_stage(
            stage, args.repository, expected_title, deadline, gh, save
        )

    def start_production():
        if args.revision_file:
            revision = create_revision(
                args.editorial_release,
                args.revision_file,
                args.output / "editorial-revision",
                publish=False,
            )
            if canonical(revision["source_url"]) != url:
                raise RuntimeError("Editorial revision belongs to another source URL")
            # Publish once after source identity is known, without regenerating anything.
            from clipforge.editorial_revision import publish_revision

            revision = publish_revision(args.output / "editorial-revision")
            args.editorial_release = revision["release"]
            state["editorial_revision"] = revision
            save()
        state["editorial_release"] = args.editorial_release
        state["mode"] = "render_only" if args.render_only else "production"
        state["reuse_render"] = args.reuse_render
        save()
        final_run, final_request = dispatch(
            "clipforge-stages.yml",
            {
                "audio_stage": "render" if args.render_only else "understanding",
                "worker": "colab-auto" if args.render_only else "cpu",
                "previous_run_id": args.analysis_run or "",
                "previous_request_id": args.analysis_request or "",
                "source_url": url,
                "config_file": args.config_file,
                "editorial_release": args.editorial_release,
                "continue_missing": args.continue_missing,
                "auto_render": not args.render_only,
                "resume_render": args.reuse_render,
            },
        )
        return final_run, final_request

    def collect(final_run, final_request):
        state["render_run_id"] = final_run
        state["render_request_id"] = final_request
        save()
        metadata = read_transfer(final_run, "clipforge-result-" + final_request)
        if canonical(metadata["stage_manifest"]["source_url"]) != url:
            raise RuntimeError("Rendered output belongs to another source URL")
        if not state.get("render_complete"):
            download(
                final_run, "clipforge-result-" + final_request, args.output / "clips"
            )
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
                    "target_clips": args.clips,
                    "clips": len(
                        metadata.get("stage_manifest", {}).get("rendered", [])
                    ),
                    "output": str(args.output / "clips"),
                    "elapsed_sec": state["elapsed_sec"],
                }
            ),
            flush=True,
        )

    try:
        if resumed is not None:
            stages = [
                s for s in state["stages"] if s["workflow"] == "clipforge-stages.yml"
            ]
            if len(stages) != 1:
                raise RuntimeError("A single saved production stage is required")
            stage = stages[0]
            if stage.get("run_id") not in (None, args.resume_existing_run):
                raise RuntimeError("Saved stage belongs to another run")
            stage["run_id"] = args.resume_existing_run
            state["status"] = "observing_existing_run"
            save()
            final_run, final_request = wait_for_stage(
                stage,
                args.repository,
                "ClipForge " + stage["request_id"],
                deadline,
                gh,
                save,
            )
        else:
            final_run, final_request = start_production()
        collect(final_run, final_request)
    except MonitoringPending:
        state["status"] = "monitoring_paused"
        save()
        raise
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
