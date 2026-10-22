"""Run bounded subagent tasks on OpenCode: free DeepSeek V4.1 Flash, or GPT-6.1 Sol.

Each run enables only its model's provider and uses that model for titles too. The free model
stops at once if any step reports a cost above zero; --model sol uses one logged-in ChatGPT
profile (CLIPFORGE_OPENAI_PROFILE) in place and records the reported cost. Read tasks may only read and search
the repository. Write tasks edit a private git worktree and may run tests and ruff there; they
can never commit, push or fetch. Each task gets its own log, report and timeout, and several
tasks run side by side.

    python tools/opencode_harness.py --mode read --task review.md --task captions.md
    python tools/opencode_harness.py --mode write --task fix.md --base HEAD
    CLIPFORGE_OPENAI_PROFILE=~/.clipforge-openai/account-1/data \
        python tools/opencode_harness.py --mode read --model sol --task design.md
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

MODEL = "opencode/deepseek-v4.1-flash-free"
# The free model is the default; GPT-6.1 Sol runs on the owner's ChatGPT subscription through
# one logged-in OpenCode profile named by CLIPFORGE_OPENAI_PROFILE (its data directory).
# A profile is never switched automatically; another one is used only when the owner says so.
MODELS = {
    "deepseek": {"id": MODEL, "provider": "opencode", "free": True},
    "sol": {
        "id": "openai/gpt-6.1-sol",
        "provider": "openai",
        "free": False,
        "variant": "high",
        "slots": 4,
    },
}
# Fixes and implementation go to GPT-6.1 Sol at high reasoning; reading stays on the free model.
DEFAULT_MODEL = {"write": "sol", "read": "sol"}
# Seven simultaneous starts left four silent on 2026-10-22; two or three answered normally.
MAX_RUNNING = 3
STALL_SECONDS = 120
START_ATTEMPTS = 3
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
    "grep *",
    "rg *",
    "ls*",
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
- Answer in Japanese. Be concise: conclusion first, then evidence as file:line.
- Stay inside this repository. Never read or print credentials, tokens, .env files or auth files.
- Never quote real speech, transcripts or private media content; use synthetic examples.
- {mode_rule}
- Final message format: ## 結論, ## 根拠 (file:line), ## 変更/提案 (minimal diff), ## 未確認.

Task:
"""
MODE_RULES = {
    "read": "Read-only: do not edit files. Propose minimal diffs in the report instead.",
    "write": "Edit only what the task needs, match the surrounding style, run the relevant "
    "tests and ruff, and report the last line of each. Do not commit.",
}


def config(mode, model="deepseek"):
    bash = {"*": "deny"}
    bash.update(
        {pattern: "allow" for pattern in (WRITE_BASH if mode == "write" else READ_BASH)}
    )
    bash.update({pattern: "deny" for pattern in DENIED_BASH})
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": MODELS[model]["id"],
        "small_model": MODELS[model]["id"],
        "enabled_providers": [MODELS[model]["provider"]],
        "share": "disabled",
        "autoupdate": False,
        "plugin": [],
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


def attempt(command, environment, workdir, out, timeout, result, texts, free=True):
    """Run OpenCode once. False only when it never produced an event within STALL_SECONDS."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
        env=environment,
        cwd=workdir,
    )
    answered = threading.Event()
    stalled = threading.Event()

    def watch():
        if not answered.wait(min(STALL_SECONDS, timeout)):
            stalled.set()
            process.kill()

    # A silent hang produces no lines, so the limits are enforced by timers, not the loop.
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    threading.Thread(target=watch, daemon=True).start()
    started = time.monotonic()
    try:
        for line in process.stdout:
            answered.set()
            out.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            part = event.get("part") or {}
            if event.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
            elif event.get("type") == "tool_use":
                result["tool_calls"] += 1
            elif event.get("type") == "step_finish":
                result["steps"] += 1
                result["tokens"] += (part.get("tokens") or {}).get("total", 0)
                result["cost"] = result.get("cost", 0) + (part.get("cost") or 0)
                if free and part.get("cost", 0):
                    result["error"] = (
                        "non-zero cost reported; stopped (free model only)"
                    )
                    return True
        process.wait(timeout=30)
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
        timer.cancel()
        if process.poll() is None:
            process.kill()
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
        raise RuntimeError(
            f"insufficient disk space in {directory}: {free} bytes free; "
            f"at least 3 GB ({MIN_FREE_BYTES} bytes) required"
        )


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
    if not MODELS[model]["free"]:
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
    command = ["opencode", "run", "--format", "json", "--dir", str(workdir)]
    command += ["-m", MODELS[model]["id"]]
    if MODELS[model].get("variant"):
        command += ["--variant", MODELS[model]["variant"]]
    command.append(prompt)
    with slot(model), log.open("w") as out:
        # Space may have been consumed by another run while this one waited for a slot.
        check_disk_space(directory)
        # A start that never answers is retried; one that has answered is never repeated.
        while result["attempts"] < START_ATTEMPTS:
            result["attempts"] += 1
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                result["error"] = "timeout"
                break
            free = MODELS[model]["free"]
            if attempt(
                command, environment, workdir, out, remaining, result, texts, free
            ):
                break
    result["seconds"] = round(time.monotonic() - started, 1)
    report = directory / "report.md"
    report.write_text(texts[-1] if texts else "", encoding="utf-8")
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
        help="deepseek (free) or sol (GPT-6.1 Sol high via one ChatGPT profile); "
        "write tasks default to sol and read tasks to deepseek",
    )
    args = parser.parse_args(argv)
    args.model = args.model or DEFAULT_MODEL[args.mode]
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
