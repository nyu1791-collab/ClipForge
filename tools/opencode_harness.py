"""Run one explicit OpenCode task with the authorized Vertex Gemini provider.

Vertex is the default for read and write tasks. Read tasks cannot edit; write tasks
use a private worktree and cannot commit or push. Each task starts the harness once.
Other providers require an explicit --model choice and are never used as fallbacks.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
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

MODEL = "opencode/deepseek-v4.1-flash-free"
# Existing providers remain available only by explicit selection.
MODELS = {
    "vertex": {
        "id": "google-vertex/gemini-3.8-flash",
        "provider": "google-vertex",
        "free": False,
        "slots": 1,
        "attempts": 1,
        "stall_seconds": None,
    },
    "deepseek": {"id": MODEL, "provider": "opencode", "free": True},
    "sol": {
        "id": "openai/gpt-6.1-sol",
        "provider": "openai",
        "free": False,
        "variant": "high",
        "slots": 4,
    },
}
# Vertex Gemini is the explicitly authorized default for this project.
DEFAULT_MODEL = {"write": "vertex", "read": "vertex"}
# Seven simultaneous starts left four silent on 2026-10-22; two or three answered normally.
MAX_RUNNING = 3
# Other routes: no first event within this many seconds is a stall. The Vertex route has
# no separate first-event limit; its guard bounds the wait for the local server, and the
# task timeout bounds the rest.
STALL_SECONDS = 120
SECRET_PATTERN = re.compile(
    r'(?i)bearer\s+\S+|ya29\.[\w.-]+|"private_key"\s*:\s*"[^"]*"'
)
MIN_FREE_BYTES = 3 * 1024**3
ROOT = Path(__file__).resolve().parents[1]
STATE = (
    Path(os.environ.get("CLIPFORGE_AGENT_STATE", tempfile.gettempdir()))
    / "opencode-agents"
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
WRITE_BASH = READ_BASH + [
    ".venv/bin/python -m pytest*",
    ".venv/bin/ruff check *",
    ".venv/bin/ruff format --check *",
    "uv run --no-sync pytest*",
    "uv run --no-sync ruff check *",
    "uv run --no-sync ruff format --check *",
]
# Listed after the allowances so that the denials win for anything that matches both.
# Absolute or home paths, in-place fixes and file output would reach outside the task's tree.
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
- Stay inside this repository. Never print credentials, tokens, .env or auth files.
- Never quote real speech, transcripts or private media; use synthetic examples.
- {mode_rule}

Task:
"""
MODE_RULES = {
    "read": "Read-only: do not edit files. Propose minimal diffs in the report instead.",
    "write": "Edit only what the task needs and match the surrounding style. "
    "Do not run audits, tests, retries or additional generation unless the user requested them. "
    "Do not commit.",
}


def config(mode, model="deepseek"):
    bash = {"*": "deny"}
    bash.update(
        {pattern: "allow" for pattern in (WRITE_BASH if mode == "write" else READ_BASH)}
    )
    bash.update({pattern: "deny" for pattern in DENIED_BASH})
    result = {
        "$schema": "https://opencode.ai/config.json",
        "model": MODELS[model]["id"],
        "small_model": MODELS[model]["id"],
        "enabled_providers": [MODELS[model]["provider"]],
        "share": "disabled",
        "autoupdate": False,
        "plugin": [],
        "agent": {"title": {"disable": True}},
        "compaction": {"auto": False},
        "permission": {
            "edit": "allow" if mode == "write" else "deny",
            "bash": bash,
            "webfetch": "deny",
            "websearch": "deny",
            "task": "deny",
            "external_directory": "deny",
            "doom_loop": "deny",
        },
    }

    if model == "vertex":
        result["provider"] = {
            "google-vertex": {
                "npm": "@ai-sdk/google-vertex",
                "options": {
                    "project": "project-fdadb4dd-cb77-4270-9cd",
                    "location": "global",
                },
                "models": {"gemini-3.8-flash": {"name": "Gemini 3.8 Flash (Vertex)"}},
            }
        }
    return result


def worktree(base, name):
    path = STATE / "worktrees" / name
    if path.exists():
        raise SystemExit(f"worktree already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "add", "--detach", str(path), base],
        check=True,
        capture_output=True,
    )
    venv = ROOT / ".venv"
    if venv.is_dir() and not (path / ".venv").exists():
        (path / ".venv").symlink_to(venv)  # tests use the lead's environment, read-only
    return path


def slot(model="deepseek"):
    """Hold one of a model's machine-wide slots: the free endpoint stalls when crowded, and
    the subscription model keeps within its usage limits."""
    directory = STATE / "slots" / model
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        for index in range(MODELS[model].get("slots", MAX_RUNNING)):
            handle = (directory / f"{index}.lock").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            return handle
        time.sleep(5)


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
    free=True,
    stall=STALL_SECONDS,
    errors=None,
    heartbeat_seconds=60,
):
    """Run OpenCode once. False only when no event arrived within the stall limit."""
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
    answered = threading.Event()
    stalled = threading.Event()
    relay = None
    if errors is not None:
        relay = threading.Thread(
            target=relay_stderr, args=(process.stderr, errors), daemon=True
        )
        relay.start()
    emit("OpenCode task started; waiting for its first event")
    last_event = [time.monotonic()]
    finished = threading.Event()

    def beat():
        # Shows that the run is alive while the model is working silently.
        while not finished.wait(heartbeat_seconds):
            quiet = int(time.monotonic() - last_event[0])
            emit(f"still running; {quiet} s since the last event")

    threading.Thread(target=beat, daemon=True).start()

    def watch():
        limit = timeout if stall is None else min(stall, timeout)
        if not answered.wait(limit):
            stalled.set()
            kill_task(process)

    # A silent hang produces no lines, so the limits are enforced by timers, not the loop.
    timer = threading.Timer(timeout, lambda: kill_task(process))
    timer.start()
    threading.Thread(target=watch, daemon=True).start()
    started = time.monotonic()
    try:
        for line in process.stdout:
            answered.set()
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
                return True
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
                if free and part.get("cost", 0):
                    result["error"] = (
                        "non-zero cost reported; stopped (free model only)"
                    )
                    return True
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # The output ended but the process did not exit; end it and keep the result.
            kill_task(process)
            process.wait()
        if stalled.is_set():
            result["error"] = "stalled before the first event"
            return False
        if process.returncode == 0 and texts:
            result["status"] = "complete"
            result.pop("error", None)
        elif time.monotonic() - started >= timeout:
            result["error"] = "timeout"
        else:
            result["error"] = f"opencode exited {process.returncode}"
        return True
    finally:
        answered.set()
        finished.set()
        timer.cancel()
        if relay is not None:
            relay.join(timeout=10)
        if process.poll() is None:
            kill_task(process)
            process.wait()


def openai_profile():
    """The logged-in OpenCode data directory for GPT-6.1 Sol, checked without reading it."""
    profile = Path(os.environ.get("CLIPFORGE_OPENAI_PROFILE", "")).expanduser()
    if not str(profile) or not (profile / "opencode" / "auth.json").is_file():
        raise SystemExit(
            "set CLIPFORGE_OPENAI_PROFILE to a logged-in OpenCode data directory"
        )
    return profile


def check_disk_space(directory):
    free = shutil.disk_usage(directory).free
    if free < MIN_FREE_BYTES:
        emit(f"low disk space in {directory}: {free} bytes free; about 3 GB is advised")


def run_task(task, mode, base, timeout, model="deepseek"):
    name = f"{Path(task).stem}-{time.strftime('%H%M%S')}-{os.getpid()}"
    directory = STATE / "runs" / name
    directory.parent.mkdir(parents=True, exist_ok=True)
    check_disk_space(directory.parent)
    directory.mkdir(parents=True, mode=0o700)
    home = directory / "home"
    result = None
    try:
        result = _run_task(task, mode, base, timeout, model, name, directory)
        return result
    finally:
        # Only disposable data owned by this run; never the profile or write worktree.
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


def _run_task(task, mode, base, timeout, model, name, directory):
    workdir = worktree(base, name) if mode == "write" else ROOT
    settings = directory / "opencode.json"
    settings.write_text(json.dumps(config(mode, model), indent=2))
    prompt = RULES.format(mode_rule=MODE_RULES[mode]) + Path(task).read_text(
        encoding="utf-8"
    )
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
    if mode == "write":
        # The shared virtualenv installs the main checkout; tests must import this worktree.
        environment["PYTHONPATH"] = str(workdir / "src")
    if model == "vertex":
        credential_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not credential_path or not Path(credential_path).is_file():
            raise RuntimeError("Vertex ADC is not connected; no model request was sent")
        environment.update(
            GOOGLE_APPLICATION_CREDENTIALS=str(Path(credential_path).resolve()),
            GOOGLE_CLOUD_PROJECT="project-fdadb4dd-cb77-4270-9cd",
            GOOGLE_CLOUD_LOCATION="global",
        )
    elif model == "sol":
        # The subscription login stays where it was made; it is used in place, never copied.
        environment["XDG_DATA_HOME"] = str(openai_profile())
    log = directory / "events.jsonl"
    started = time.monotonic()
    result = {
        "task": str(task),
        "mode": mode,
        "model": MODELS[model]["id"],
        "workdir": str(workdir),
        "log": str(log),
        "status": "failed",
        "steps": 0,
        "tool_calls": 0,
        "tokens": 0,
        "attempts": 0,
    }
    texts = []
    binary = os.environ.get("CLIPFORGE_OPENCODE_BIN") or shutil.which("opencode")
    if not binary:
        local = ROOT.parent / ".tools/opencode/node_modules/.bin/opencode"
        if local.is_file():
            binary = str(local)
        else:
            raise RuntimeError("Install opencode-ai@1.19.4 before invoking the harness")
    command = [binary, "run", "--format", "json", "--dir", str(workdir)]
    command += ["-m", MODELS[model]["id"]]
    if MODELS[model].get("variant"):
        command += ["--variant", MODELS[model]["variant"]]
    command.append(prompt)
    if model == "vertex":
        command = [
            sys.executable,
            str(ROOT / "tools/opencode_vertex_guard.py"),
            binary,
            "--dir",
            str(workdir),
            prompt,
        ]
    stderr_log = directory / "stderr.log"
    with (
        slot(model),
        log.open("w") as out,
        stderr_log.open("w", encoding="utf-8") as errors,
    ):
        # Space may have been consumed by another run while this one waited for a slot.
        check_disk_space(directory)
        # One harness start per task; an unanswered start is not retried.
        while result["attempts"] < MODELS[model].get("attempts", 1):
            result["attempts"] += 1
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                result["error"] = "timeout"
                break
            free = MODELS[model]["free"]
            if attempt(
                command,
                environment,
                workdir,
                out,
                remaining,
                result,
                texts,
                free,
                stall=MODELS[model].get("stall_seconds", STALL_SECONDS),
                errors=errors,
            ):
                break
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
    if mode == "write":
        diff = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--stat"],
            capture_output=True,
            text=True,
        )
        result["diff_stat"] = diff.stdout.strip().splitlines()[-1:] or ["no changes"]
    (directory / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["read", "write"], required=True)
    parser.add_argument(
        "--task", action="append", required=True, help="task file (repeatable)"
    )
    parser.add_argument("--base", default="HEAD", help="commit for write worktrees")
    parser.add_argument("--timeout", type=int, default=900, help="seconds per task")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument(
        "--model",
        choices=sorted(MODELS),
        help="vertex (Gemini 3.8 Flash, default), deepseek, or sol; no automatic model fallback",
    )
    args = parser.parse_args(argv)
    args.model = args.model or DEFAULT_MODEL[args.mode]
    if args.model == "vertex" and len(args.task) != 1:
        parser.error(
            "Vertex harness accepts one task per invocation; no implicit extra generation"
        )
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        results = list(
            pool.map(
                lambda task: run_task(
                    task, args.mode, args.base, args.timeout, args.model
                ),
                args.task,
            )
        )
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if all(r["status"] == "complete" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
