#!/usr/bin/env python3
"""Prepare a private human review package, or render an explicitly approved selection."""

import argparse
import json
import os
from pathlib import Path
import re
import time
import uuid

from clipforge.editorial_revision import EDITORIAL_TAG_PATTERN, create_revision
from clipforge.actions_monitor import MonitoringPending, wait_for_stage
from clipforge.github_cli import run as github_run
from clipforge.media_sources import canonical
from clipforge.release_transfer import download, read_transfer, validate_identity
from clipforge.storage import write_json


def gh(*args, body=None):
    return github_run(
        *args,
        body=body,
        message="GitHub operation failed; reconnect to the saved request, no resubmission",
        include_http_status=True,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", nargs="?")
    ap.add_argument("--output", type=Path)
    ap.add_argument(
        "--connections",
        action="store_true",
        help="Show connection registration and setup links; no model or GPU work",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Reconnect using production-state.json; no URL or run ID required",
    )
    ap.add_argument(
        "--resume-existing-run",
        type=int,
        help="Observe and collect this saved production run; never dispatch another run",
    )
    ap.add_argument(
        "--source-ref",
        help="Required exact 40-character source commit for a new dispatch",
    )
    ap.add_argument(
        "--repository",
        choices=["nyu1791-collab/ClipForge", "nyu1791-collab/ai-model-workers"],
        default="nyu1791-collab/ClipForge",
    )
    ap.add_argument("--workflow-ref", default="main")
    ap.add_argument("--editorial-release", default="")
    ap.add_argument("--continue-missing", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--review-approved", action="store_true", help="Confirm the saved selection was approved for rendering")
    ap.add_argument("--review-state", type=Path, help="Private production-state.json of the reviewed package")
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
    # The user's normal production target is 360p for both retrieval and output.
    # Explicit profiles still opt into the 720p or 1080p text canvases.
    ap.add_argument(
        "--config-file", help="Explicit committed configuration for a new dispatch"
    )
    ap.add_argument(
        "--render-profile",
        choices=["preview", "standard", "full-hd"],
        help="preview: 360p (default) / standard: 720p / full-hd: 1080p; source <=360p",
    )
    ap.add_argument(
        "--deliver-drive",
        action="store_true",
        help="Upload once to the explicitly configured target Drive after rendering",
    )
    ap.add_argument("--timeout", type=int, default=7200)
    args = ap.parse_args()
    if args.connections:
        from clipforge.connections import describe_connections

        print(
            json.dumps(
                describe_connections(args.repository), ensure_ascii=False, indent=2
            )
        )
        return
    if args.output is None:
        ap.error("output is required for production or resume")
    resumed = None
    if args.resume or args.resume_existing_run is not None:
        if args.resume_existing_run is not None and args.resume_existing_run <= 0:
            ap.error("resume-existing-run requires a positive run ID")
        resumed = json.loads((args.output / "production-state.json").read_text())
        if args.url and canonical(resumed["source_url"]) != canonical(args.url):
            ap.error("saved production belongs to another source URL")
        if args.source_ref and args.source_ref != resumed["source_ref"]:
            ap.error("saved production uses another source commit")
        if (
            args.revision_file
            or args.continue_missing
            or args.deliver_drive
            or args.render_profile
            or args.render_only
            or args.review_approved
            or args.review_state
            or args.editorial_release
            or args.reuse_render
            or args.analysis_run
            or args.analysis_request
            or args.clips is not None
        ):
            ap.error(
                "resume-existing-run only observes and collects the saved production"
            )
        args.url = resumed["source_url"]
        args.source_ref = resumed["source_ref"]
        args.repository = resumed["repository"]
        args.config_file = resumed["config_file"]
        args.workflow_ref = resumed.get("workflow_ref", args.workflow_ref)
        args.clips = resumed.get("target_clips", 5)
        args.deliver_drive = resumed.get("deliver_drive", False) or any(
            s["workflow"] == "clipforge-drive-delivery.yml" for s in resumed["stages"]
        )
    if not args.url:
        ap.error(
            "a source URL is required for a new production; use --resume to reconnect"
        )
    if resumed is None and args.clips is None:
        ap.error("clips must be explicitly specified before dispatch")
    if resumed is None and not (args.config_file or args.render_profile):
        ap.error("config-file or render-profile must be explicitly specified before dispatch")
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
    if args.render_only and not args.review_approved:
        ap.error("render-only requires explicit --review-approved")
    if args.render_only and not args.review_state:
        ap.error("render-only requires the reviewed package's --review-state")
    if args.review_state and not args.render_only:
        ap.error("review-state requires render-only")
    if args.review_approved and not args.render_only:
        ap.error("review-approved requires render-only")
    if resumed is None and args.deliver_drive and not args.render_only:
        ap.error("Drive delivery is available only after review and explicit render-only")
    if args.continue_missing and not args.editorial_release:
        ap.error("continue-missing requires editorial-release")
    if args.render_only and not (
        args.editorial_release and args.analysis_run and args.analysis_request
    ):
        ap.error(
            "render-only requires editorial-release and the saved run/request pair"
        )
    if args.render_only:
        reviewed = json.loads(args.review_state.read_text())
        if (reviewed.get("status") != "awaiting_review"
            or reviewed.get("editorial_release") != args.editorial_release
            or canonical(reviewed.get("source_url")) != canonical(args.url)
            or (not args.reuse_render and
                (str(reviewed.get("review_run_id")) != args.analysis_run
                 or reviewed.get("review_request_id") != args.analysis_request))):
            ap.error("render selection does not match the reviewed package")
    if args.repository != "nyu1791-collab/ClipForge":
        ap.error("new audio-understanding production runs from ClipForge")
    if not args.source_ref:
        ap.error("source-ref is required and must identify the requested code commit")
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
    settings = (
        {"height": resumed.get("render_height")}
        if resumed is not None
        else json.loads(
            (Path(__file__).resolve().parents[1] / args.config_file).read_text()
        )
    )
    requested_count = args.clips if args.clips is not None else settings.get("clips", 5)
    args.clips = requested_count
    if not 1 <= args.clips <= 10:
        ap.error("clips must be a target between 1 and 10; actual output may be fewer")
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
            "workflow_ref": args.workflow_ref,
            "config_file": args.config_file,
            "render_height": settings.get("height"),
            "source_height_limit": 360,
            "target_clips": args.clips,
            "deliver_drive": args.deliver_drive,
            "started_at": time.time(),
            "stages": [],
        }
    )
    deadline = time.monotonic() + args.timeout

    def save():
        write_json(statefile, state)

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
        state["mode"] = "render_only" if args.render_only else "review"
        if args.render_only:
            state["review_approved"] = True
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
                "auto_render": False,
                "review_approved": args.review_approved,
                "resume_render": args.reuse_render,
                "target_clips": str(args.clips),
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
            local = args.output / "clips"
            if local.exists():
                # Collection may have finished immediately before the local connection
                # disappeared. Reconnect by identity, without rescanning the videos.
                validate_identity(
                    local,
                    final_run,
                    "clipforge-result-" + final_request,
                    args.source_ref,
                    audit_hashes=False,
                )
            elif (args.output / "completed").is_dir() and any(
                (args.output / "completed").glob("clip-*")
            ):
                # Preserve earlier per-clip downloads; collect only missing clips and
                # assemble the final bundle from the verified transfer records.
                from clipforge.streaming_collection import assemble_collected

                assemble_collected(
                    args.output, final_run, final_request, args.source_ref, metadata
                )
            else:
                download(final_run, "clipforge-result-" + final_request, local)
        state["render_complete"] = True
        save()
        if args.deliver_drive and state.get("drive_status") != "uploaded":
            deliveries = [
                s
                for s in state["stages"]
                if s["workflow"] == "clipforge-drive-delivery.yml"
            ]
            if deliveries:
                delivery = deliveries[-1]
                drive_run, _ = wait_for_stage(
                    delivery,
                    args.repository,
                    delivery["expected_title"],
                    deadline,
                    gh,
                    save,
                )
            else:
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
            if args.resume_existing_run is not None and stage.get("run_id") not in (
                None,
                args.resume_existing_run,
            ):
                raise RuntimeError("Saved stage belongs to another run")
            if args.resume_existing_run is not None:
                stage["run_id"] = args.resume_existing_run
            state["status"] = "observing_existing_run"
            save()
            if state.get("render_complete"):
                final_run, final_request = (
                    state["render_run_id"],
                    state["render_request_id"],
                )
            else:
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
        if state.get("mode") == "review":
            from clipforge.review_package import collect_package

            package = collect_package(final_run, args.output, state, save)
            state.update(status="awaiting_review", review_run_id=final_run,
                         review_request_id=final_request,
                         editorial_release=f"clipforge-kick-editorial-{final_run}")
            save()
            print(json.dumps({"status": "awaiting_review", "review_package": str(package),
                              "editorial_release": state["editorial_release"],
                              "run_id": final_run, "request_id": final_request}), flush=True)
        else:
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
