#!/usr/bin/env python3
"""Idle VM intake; CPU/video/agent work is dispatched to one private Actions run."""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import subprocess
import threading
import time
import uuid

from url_common import DEEPSEEK_MODEL, GEMINI_MODEL, WORKER, youtube_url

SOCKET = "/run/clipforge-agent/bridge.sock"
STATE = Path("/var/lib/clipforge-agent")
CONFIG = Path("/etc/clipforge-agent/config.json")
WORKFLOW = "clipforge-url.yml"
LOCK = threading.RLock()


def gh(args, timeout=90):
    result = subprocess.run(["gh", *args], capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Private Actions operation failed")
    return result.stdout


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def load(job_id):
    if not re.fullmatch("[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job ID")
    return json.loads((STATE / (job_id + ".json")).read_text())


def jobs():
    return sorted(
        (json.loads(p.read_text()) for p in STATE.glob("*.json")),
        key=lambda j: j["created_at"],
    )


def save(job):
    with LOCK:
        write(STATE / (job["id"] + ".json"), job)


def collect(job):
    # Private, small diagnostic/patch only. Video stays in private Actions artifacts.
    output = STATE / job["id"]
    output.mkdir(exist_ok=True)
    artifacts = ["url-helper-" + job["id"]]
    if job["mode"] == "video":
        artifacts += ["url-diagnostic-" + job["id"]]
    for name in artifacts:
        directory = output / name
        if directory.exists():
            continue
        try:
            gh(
                [
                    "run",
                    "download",
                    str(job["run_id"]),
                    "--repo",
                    WORKER,
                    "--name",
                    name,
                    "--dir",
                    str(directory),
                ]
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            job["collection_incomplete"] = True
    for name, filename in [
        ("helper", "agent-receipt.json"),
        ("diagnostic", "diagnostic.json"),
    ]:
        files = list(output.glob("*/" + filename))
        if files and files[0].stat().st_size <= 128 * 1024:
            value = json.loads(files[0].read_text())
            if (
                value.get("request_id") == job["id"]
                and value.get("source_ref") == job["source_ref"]
            ):
                job[name] = value
            else:
                job["collection_incomplete"] = True


def reconcile(job):
    runs = json.loads(
        gh(
            [
                "run",
                "list",
                "--repo",
                WORKER,
                "--workflow",
                WORKFLOW,
                "--limit",
                "100",
                "--json",
                "databaseId,displayTitle,status,conclusion",
            ]
        )
    )
    matching = [r for r in runs if r["displayTitle"] == "ClipForge URL " + job["id"]]
    if len(matching) > 1:
        job.update(status="needs_reconciliation", reason="duplicate_run_identity")
        save(job)
        return
    if not matching:
        # dispatch could have succeeded even if the CLI connection died. Never resend.
        if time.time() - job.get("dispatch_at", 0) > 180:
            job.update(status="needs_reconciliation", reason="dispatch_outcome_unknown")
            save(job)
        return
    run = matching[0]
    job.update(
        run_id=run["databaseId"],
        run_url=f"https://github.com/{WORKER}/actions/runs/{run['databaseId']}",
    )
    if run["status"] != "completed":
        job.update(status="running")
    else:
        job.update(
            status="complete" if run["conclusion"] == "success" else "failed",
            conclusion=run["conclusion"],
        )
        collect(job)
    save(job)


def tick():
    active = [j for j in jobs() if j["status"] in ("dispatching", "running")]
    if active:
        reconcile(active[0])
        return
    # Uncertain dispatch blocks new work until an operator reconciles it.
    if any(j["status"] == "needs_reconciliation" for j in jobs()):
        return
    queued = [j for j in jobs() if j["status"] == "queued"]
    if not queued:
        return
    job = queued[0]
    job.update(status="dispatching", dispatch_at=time.time())
    save(job)  # durable checkpoint BEFORE any external write
    try:
        gh(
            [
                "workflow",
                "run",
                WORKFLOW,
                "--repo",
                WORKER,
                "--ref",
                "main",
                "-f",
                "source_ref=" + job["source_ref"],
                "-f",
                "request_id=" + job["id"],
                "-f",
                "mode=" + job["mode"],
                "-f",
                "video_url=" + job.get("url", ""),
            ]
        )
    except (RuntimeError, subprocess.TimeoutExpired):
        pass  # reconcile, never repeat workflow_dispatch automatically


def handle(request, uid):
    config = json.loads(CONFIG.read_text())
    if uid not in config["allowed_uids"]:
        raise PermissionError("Caller is not authorized")
    op = request.get("op")
    with LOCK:
        if op == "ping":
            return {
                "service": "clipforge-agent",
                "source_ref": config["source_ref"],
                "worker": WORKER,
                "helper_model": DEEPSEEK_MODEL,
                "selection_model": GEMINI_MODEL,
                "idle_inference": False,
                "executes_on_vm": False,
                "queued": sum(j["status"] == "queued" for j in jobs()),
            }
        if op == "list":
            return [
                {"id": j["id"], "mode": j["mode"], "status": j["status"]}
                for j in jobs()[-30:]
            ]
        if op in ("status", "result"):
            job = load(request.get("id", ""))
            return job
        if op in ("submit", "fixture"):
            if (
                sum(j["status"] in ("queued", "running", "dispatching") for j in jobs())
                >= 4
            ):
                raise ValueError("Queue is full")
            if not re.fullmatch("[a-f0-9]{40}", config["source_ref"]):
                raise ValueError("Invalid configured revision")
            job = {
                "id": uuid.uuid4().hex,
                "source_ref": config["source_ref"],
                "created_at": time.time(),
                "caller_uid": uid,
                "status": "queued",
                "mode": "fixture" if op == "fixture" else "video",
            }
            if op == "submit":
                job["url"] = youtube_url(request.get("url"))
            save(job)
            return job
    raise ValueError("Unsupported operation")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        uid = struct.unpack(
            "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[1]
        try:
            raw = self.rfile.readline(8193)
            if len(raw) > 8192:
                raise ValueError("Request is too large")
            value = handle(json.loads(raw), uid)
            response = {"ok": True, "result": value}
        except Exception as error:
            response = {"ok": False, "error": type(error).__name__}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(SOCKET).unlink(missing_ok=True)
    with Server(SOCKET, Handler) as server:
        os.chmod(SOCKET, 0o660)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        while True:
            try:
                tick()
            except Exception:
                # Never print remote response bodies or signed artifact URLs.
                print("ClipForge intake polling failed; no redispatch", flush=True)
            time.sleep(20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["serve", "ping", "list", "submit", "fixture", "status", "result"],
    )
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    if args.operation == "serve":
        serve()
        return
    request = {"op": args.operation}
    if args.operation == "submit":
        request["url"] = youtube_url(args.value)
    elif args.operation in ("status", "result"):
        request["id"] = args.value
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(SOCKET)
        client.sendall(json.dumps(request).encode() + b"\n")
        with client.makefile("rb") as reader:
            response = json.load(reader)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    if not response["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
