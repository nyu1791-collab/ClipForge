"""Observe an existing Actions request without retrying dispatch or model work."""

import json
import subprocess
import time


class MonitoringPending(RuntimeError):
    """The remote run may still be active; reconnect instead of submitting again."""


def wait_for_stage(stage, repository, expected_title, deadline, gh, save):
    last_notice = 0
    while time.monotonic() < deadline:
        try:
            if not stage.get("run_id"):
                runs = json.loads(
                    gh(
                        "run",
                        "list",
                        "--repo",
                        repository,
                        "--workflow",
                        stage["workflow"],
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
                    raise ValueError("Multiple runs have the saved request identity")
                if found:
                    stage["run_id"] = found[0]["databaseId"]
            result = (
                json.loads(
                    gh(
                        "run",
                        "view",
                        str(stage["run_id"]),
                        "--repo",
                        repository,
                        "--json",
                        "status,conclusion,url,displayTitle",
                    )
                )
                if stage.get("run_id")
                else None
            )
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
            # Only reads are retried. Keep the last known remote state intact.
            stage["monitor_status"] = "temporarily_unavailable"
            stage["monitor_error_type"] = type(error).__name__
            result = None
        else:
            stage["monitor_status"] = (
                "observing" if result else "awaiting_request_identity"
            )
        if result:
            # The generic workflow title can precede input expansion. Never guess
            # another request's ID from its timestamp or queue position.
            if result["displayTitle"] != expected_title:
                if result["status"] != "queued":
                    raise ValueError("Run does not match the saved request identity")
                stage["monitor_status"] = "awaiting_request_identity"
            else:
                stage.update({k: result[k] for k in ("status", "conclusion", "url")})
                if result["status"] == "in_progress":
                    stage.setdefault("first_running_observed_at", time.time())
                if result["status"] == "completed":
                    stage["elapsed_sec"] = round(time.time() - stage["started_at"], 3)
                    save()
                    if result["conclusion"] != "success":
                        raise RuntimeError("Stage failed; no retry or model fallback")
                    return str(stage["run_id"]), stage["request_id"]
        save()
        if time.monotonic() - last_notice >= 60:
            print(
                json.dumps(
                    {
                        "workflow": stage["workflow"],
                        "run_id": stage.get("run_id"),
                        "status": stage.get("status"),
                        "monitor_status": stage["monitor_status"],
                        "elapsed_sec": round(time.time() - stage["started_at"], 1),
                        "resubmitted": False,
                    }
                ),
                flush=True,
            )
            last_notice = time.monotonic()
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    stage["monitor_status"] = "paused_timeout"
    save()
    raise MonitoringPending(
        "Monitoring paused; reconnect to the saved run, no resubmission"
    )
