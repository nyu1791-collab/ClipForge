"""Run one read-only OpenCode task with Vertex Gemini 3.8 Flash.

Each invocation runs one task once. The guard stops the run when OpenCode asks for an
API retry, so a request is never sent twice.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

MODEL_ID = "google-vertex/gemini-3.8-flash"
PROJECT = "project-fdadb4dd-cb77-4270-9cd"
MIN_FREE_BYTES = 3 * 1024**3
ROOT = Path(__file__).resolve().parents[1]
STATE = (
    Path(os.environ.get("CLIPFORGE_AGENT_STATE", tempfile.gettempdir()))
    / "opencode-agents"
)
SECRET_PATTERN = re.compile(
    r'(?i)bearer\s+\S+|ya29\.[\w.-]+|"private_key"\s*:\s*"[^"]*"'
)

READ_BASH = [
    "git diff*",
    "git log*",
    "git show*",
    "git status*",
    "git grep*",
    "grep *",
    "rg *",
    "ls*",
    "find *",
    "cat *",
    "head *",
    "tail *",
    "sed -n *",
    "wc *",
]
# Listed after the allowances so that the denials win for anything that matches both.
# Absolute or home paths, writes and network tools stay refused.
DENIED_BASH = [
    "* /*",
    "*~*",
    "*--fix*",
    "*--output*",
    "*>*",
    "*-exec*",
    "*-delete*",
    "*-fprint*",
    "*-fls*",
    "*--pre*",
    "sed -i*",
    "*sed*w*",
    "*tee *",
    # The runner keeps the Vertex credential file one level above the checkout.
    "* ..*",
    "*/..*",
    "*gha-creds*",
    "*/proc*",
    "git commit*",
    "git push*",
    "git fetch*",
    "git pull*",
    "git reset*",
    "git checkout*",
    "git clean*",
    "rm *",
    "curl *",
    "wget *",
    "gh *",
    "opencode *",
    "pip *",
    "npm *",
]

RULES = """You are a subagent working for a lead engineer. Rules:
- Answer in Japanese: the conclusion first, then the evidence as file:line.
- Read-only: do not edit files. Propose minimal diffs in the report instead.
- Stay inside this repository. Never print credentials, tokens, .env or auth files.
- Never quote real speech, transcripts or private media; use synthetic examples.

Task:
"""


def config():
    bash = {"*": "deny"}
    bash.update({pattern: "allow" for pattern in READ_BASH})
    bash.update({pattern: "deny" for pattern in DENIED_BASH})
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL_ID,
        "small_model": MODEL_ID,
        "enabled_providers": ["google-vertex"],
        "share": "disabled",
        "autoupdate": False,
        "plugin": [],
        "agent": {"title": {"disable": True}},
        "compaction": {"auto": False},
        "permission": {
            "edit": "deny",
            "bash": bash,
            "webfetch": "deny",
            "websearch": "deny",
            "task": "deny",
            "external_directory": "deny",
            "doom_loop": "deny",
        },
        "provider": {
            "google-vertex": {
                "npm": "@ai-sdk/google-vertex",
                "options": {"project": PROJECT, "location": "global"},
                "models": {"gemini-3.8-flash": {"name": "Gemini 3.8 Flash (Vertex)"}},
            }
        },
    }


def kill_task(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def redacted_tail(path, limit=2000):
    """Last part of a private stderr log, with anything credential-like removed."""
    text = path.read_text(encoding="utf-8", errors="replace")[-limit:]
    return SECRET_PATTERN.sub("[REDACTED]", text)


def emit(message):
    # This repository's step log is public, so every progress line is redacted and short.
    print(SECRET_PATTERN.sub("[REDACTED]", message)[:300], file=sys.stderr, flush=True)


def tool_input(part):
    state = part.get("state") or {}
    return json.dumps(state.get("input", {}), ensure_ascii=False)[:120]


def relay_stderr(pipe, log):
    # Guard and server messages go to the private stderr log and to the step log.
    for line in pipe:
        log.write(line)
        log.flush()
        emit("guard: " + line.rstrip())


def attempt(
    command,
    environment,
    workdir,
    out,
    timeout,
    result,
    texts,
    errors=None,
    heartbeat_seconds=60,
):
    """Run OpenCode once through the guard, until it exits or the timeout ends it."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if errors is not None else subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
        env=environment,
        cwd=workdir,
        start_new_session=True,
    )
    finished = threading.Event()
    last_event = [time.monotonic()]
    relay = None
    if errors is not None:
        relay = threading.Thread(
            target=relay_stderr, args=(process.stderr, errors), daemon=True
        )
        relay.start()
    emit("OpenCode task started; waiting for its first event")

    def beat():
        # Shows that the run is alive while the model is working silently.
        while not finished.wait(heartbeat_seconds):
            quiet = int(time.monotonic() - last_event[0])
            emit(f"still running; {quiet} s since the last event")

    threading.Thread(target=beat, daemon=True).start()
    # A silent hang produces no lines, so the timeout is enforced by a timer.
    timer = threading.Timer(timeout, lambda: kill_task(process))
    timer.start()
    started = time.monotonic()
    try:
        for line in process.stdout:
            last_event[0] = time.monotonic()
            out.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            part = event.get("part") or {}
            kind = event.get("type")
            if kind == "text" and part.get("text"):
                texts.append(part["text"])
                emit(f"text received: {len(part['text'])} characters")
            elif kind == "retry_stopped":
                result["error"] = event["reason"]
                emit(f"stopped: {event['reason']}")
                return
            elif kind == "tool_use":
                result["tool_calls"] += 1
                emit(f"tool {part.get('tool', '?')} {tool_input(part)}")
            elif kind == "step_finish":
                result["steps"] += 1
                result["tokens"] += (part.get("tokens") or {}).get("total", 0)
                result["cost"] = result.get("cost", 0) + (part.get("cost") or 0)
                emit(
                    f"step {result['steps']} finished; tokens so far {result['tokens']}"
                )
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # The output ended but the process did not exit; end it and keep the result.
            kill_task(process)
            process.wait()
        if process.returncode == 0 and texts:
            result["status"] = "complete"
            result.pop("error", None)
        elif time.monotonic() - started >= timeout:
            result["error"] = "timeout"
        else:
            result["error"] = f"opencode exited {process.returncode}"
    finally:
        finished.set()
        timer.cancel()
        if relay is not None:
            relay.join(timeout=10)
        if process.poll() is None:
            kill_task(process)
            process.wait()


def check_disk_space(directory):
    free = shutil.disk_usage(directory).free
    if free < MIN_FREE_BYTES:
        emit(f"low disk space in {directory}: {free} bytes free; about 3 GB is advised")


def run_task(task, timeout=1800):
    name = f"{Path(task).stem}-{time.strftime('%H%M%S')}-{os.getpid()}"
    directory = STATE / "runs" / name
    directory.parent.mkdir(parents=True, exist_ok=True)
    check_disk_space(directory.parent)
    directory.mkdir(parents=True, mode=0o700)
    home = directory / "home"
    result = None
    try:
        result = _run_task(task, timeout, directory)
        return result
    finally:
        # Only disposable data owned by this run; never the credential file.
        root = directory.resolve()
        target = home.resolve()
        if target == root or not target.is_relative_to(root):
            warning = "refusing to remove home outside the run directory"
            if result is not None:
                result["cleanup_warning"] = warning
                (directory / "result.json").write_text(
                    json.dumps(result, indent=2, ensure_ascii=False)
                )
            else:
                (directory / "result.json").write_text(
                    json.dumps({"cleanup_warning": warning}, indent=2)
                )
        elif home.is_symlink():
            home.unlink()
        elif home.exists():
            shutil.rmtree(home)


def _run_task(task, timeout, directory):
    settings = directory / "opencode.json"
    settings.write_text(json.dumps(config(), indent=2))
    prompt = RULES + Path(task).read_text(encoding="utf-8")
    # Only what OpenCode needs to run and reach its API; no tokens, keys or user config.
    keep = (
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
    )
    environment = {key: value for key, value in os.environ.items() if key in keep}
    home = directory / "home"
    home.mkdir(mode=0o700)
    environment.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        OPENCODE_CONFIG=str(settings),
    )
    credential_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not credential_path or not Path(credential_path).is_file():
        raise RuntimeError("Vertex ADC is not connected; no model request was sent")
    environment.update(
        GOOGLE_APPLICATION_CREDENTIALS=str(Path(credential_path).resolve()),
        GOOGLE_CLOUD_PROJECT=PROJECT,
        GOOGLE_CLOUD_LOCATION="global",
    )
    binary = os.environ.get("CLIPFORGE_OPENCODE_BIN") or shutil.which("opencode")
    if not binary:
        local = ROOT.parent / ".tools/opencode/node_modules/.bin/opencode"
        if local.is_file():
            binary = str(local)
        else:
            raise RuntimeError("Install opencode-ai@1.19.4 before invoking the harness")
    command = [
        sys.executable,
        str(ROOT / "tools/opencode_vertex_guard.py"),
        binary,
        "--dir",
        str(ROOT),
        prompt,
    ]
    log = directory / "events.jsonl"
    stderr_log = directory / "stderr.log"
    started = time.monotonic()
    result = {
        "task": str(task),
        "model": MODEL_ID,
        "workdir": str(ROOT),
        "log": str(log),
        "status": "failed",
        "steps": 0,
        "tool_calls": 0,
        "tokens": 0,
    }
    texts = []
    with log.open("w") as out, stderr_log.open("w", encoding="utf-8") as errors:
        check_disk_space(directory)
        attempt(command, environment, ROOT, out, timeout, result, texts, errors=errors)
    result["seconds"] = round(time.monotonic() - started, 1)
    if stderr_log.stat().st_size:
        result["stderr_tail"] = redacted_tail(stderr_log)
    report = directory / "report.md"
    if texts:
        # Keep every text part; the final answer may arrive in more than one piece.
        report_text = "\n\n".join(texts)
    else:
        # An empty file cannot be uploaded as a release asset, so the report says why.
        reason = result.get("error", "none")
        report_text = f"No model report. status={result['status']} error={reason}\n"
        if "stderr_tail" in result:
            report_text += f"stderr, redacted tail:\n{result['stderr_tail']}\n"
    report.write_text(report_text, encoding="utf-8")
    result["report"] = str(report)
    (directory / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", required=True, help="task file; one per invocation")
    parser.add_argument(
        "--timeout", type=int, default=1800, help="seconds for the task"
    )
    args = parser.parse_args(argv)
    result = run_task(args.task, args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
