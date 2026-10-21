"""Bounded native OpenCode session; terminate on retry, model drift or permission asks."""

import base64
import json
import os
import re
import secrets
import select
import signal
import subprocess
import threading
import time
import urllib.request

from url_common import DEEPSEEK_MODEL

CLI_VERSION = "1.19.0"
MAX_STEPS = 6


def consume(events, session_id):
    assistants, parts = set(), {}
    starts = 0
    finished = False
    for raw in events:
        if not raw.startswith(b"data:"):
            continue
        event = json.loads(raw[5:])
        prop = event.get("properties", {})
        part, info = prop.get("part", {}), prop.get("info", {})
        owner = prop.get("sessionID") or part.get("sessionID") or info.get("sessionID")
        if owner != session_id:
            continue
        kind = event.get("type")
        if kind == "session.error":
            raise RuntimeError("OpenCode generation failed")
        if kind == "permission.asked":
            raise RuntimeError("Unapproved tool permission; session stopped")
        if kind == "session.status":
            status = prop.get("status", {}).get("type")
            if status == "retry":
                # OpenCode emits this before its backoff timer; caller kills the process group.
                raise RuntimeError("OpenCode retry blocked")
            if status == "idle":
                break
        if kind == "message.updated" and info.get("role") == "assistant":
            if info.get("providerID") + "/" + info.get("modelID") != DEEPSEEK_MODEL:
                raise RuntimeError("Unexpected model identity")
            assistants.add(info["id"])
        if kind != "message.part.updated" or part.get("messageID") not in assistants:
            continue
        if part.get("type") == "step-start" and part["id"] not in parts:
            starts += 1
            # Six tool-capable turns plus the CLI's final text-only summary turn.
            if starts > MAX_STEPS + 1:
                raise RuntimeError("Agent turn budget exceeded")
        if part.get("type") == "tool" and starts > MAX_STEPS:
            raise RuntimeError("Agent turn budget exceeded")
        parts[part["id"]] = part
        if part.get("type") == "step-finish":
            finished = part.get("reason") == "stop"
    if not starts or not finished:
        raise RuntimeError("Incomplete agent session")
    tools = [
        {"tool": p.get("tool"), "status": p.get("state", {}).get("status")}
        for p in parts.values()
        if p.get("type") == "tool"
    ]
    text = "\n".join(
        p.get("text", "") for p in parts.values() if p.get("type") == "text"
    )
    return tools, text, starts


def session(root, env, prompt, timeout=480):
    env = dict(env)
    password = secrets.token_urlsafe(32)
    env.update(OPENCODE_SERVER_USERNAME="clipforge", OPENCODE_SERVER_PASSWORD=password)
    version = subprocess.run(
        ["opencode", "--version"],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=15,
        check=True,
    )
    if version.stdout.decode().strip() != CLI_VERSION:
        raise RuntimeError("Unverified OpenCode CLI version")
    process = subprocess.Popen(
        ["opencode", "serve", "--pure", "--hostname", "127.0.0.1", "--port", "0"],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def stop():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, stop)
    timer.start()
    try:
        deadline = time.monotonic() + 45
        base = None
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([process.stdout], [], [], 0.1)[0]:
                match = re.search(
                    rb"http://127\.0\.0\.1:(\d+)", process.stdout.readline()
                )
                if match:
                    base = "http://127.0.0.1:" + match[1].decode()
                    break
        if not base:
            raise RuntimeError("OpenCode server startup failed")
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        auth = "Basic " + base64.b64encode(("clipforge:" + password).encode()).decode()

        def request(path, data=None):
            return local.open(
                urllib.request.Request(
                    base + path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization": auth, "Content-Type": "application/json"},
                ),
                timeout=timeout,
            )

        with request("/session", {"title": "ClipForge bounded helper"}) as response:
            sid = json.load(response)["id"]
        with request("/event") as events:
            with request(
                "/session/" + sid + "/prompt_async",
                {
                    "agent": "clipforge-helper",
                    "model": {
                        "providerID": "opencode",
                        "modelID": DEEPSEEK_MODEL.split("/", 1)[1],
                    },
                    "parts": [{"type": "text", "text": prompt}],
                },
            ):
                pass
            return consume(events, sid)
    finally:
        timer.cancel()
        stop()
        process.wait()
        process.stdout.close()
