"""Run bounded subagent tasks on OpenCode's free DeepSeek V4.1 Flash model.

The model is fixed: no other provider is enabled, the title model is the same free model, and
a run stops at once if any step reports a cost above zero. Read tasks may only read and search
the repository. Write tasks edit a private git worktree and may run tests and ruff there; they
can never commit, push or fetch. Each task gets its own log, report and timeout, and several
tasks run side by side.

    python tools/opencode_harness.py --mode read --task review.md --task captions.md
    python tools/opencode_harness.py --mode write --task fix.md --base HEAD
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

MODEL = "opencode/deepseek-v4.1-flash-free"
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


def config(mode):
    bash = {"*": "deny"}
    bash.update(
        {pattern: "allow" for pattern in (WRITE_BASH if mode == "write" else READ_BASH)}
    )
    bash.update({pattern: "deny" for pattern in DENIED_BASH})
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL,
        "small_model": MODEL,
        "enabled_providers": ["opencode"],
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


def run_task(task, mode, base, timeout):
    name = f"{Path(task).stem}-{time.strftime('%H%M%S')}-{os.getpid()}"
    directory = STATE / "runs" / name
    directory.mkdir(parents=True, mode=0o700)
    workdir = worktree(base, name) if mode == "write" else ROOT
    settings = directory / "opencode.json"
    settings.write_text(json.dumps(config(mode), indent=2))
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
    log = directory / "events.jsonl"
    started = time.monotonic()
    result = {
        "task": str(task),
        "mode": mode,
        "model": MODEL,
        "workdir": str(workdir),
        "log": str(log),
        "status": "failed",
        "steps": 0,
        "tool_calls": 0,
        "tokens": 0,
    }
    texts = []
    with log.open("w") as out:
        process = subprocess.Popen(
            [
                "opencode",
                "run",
                "--format",
                "json",
                "--dir",
                str(workdir),
                "-m",
                MODEL,
                prompt,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            env=environment,
            cwd=workdir,
        )
        # A silent hang produces no lines, so the timeout is enforced by a timer, not the loop.
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        try:
            for line in process.stdout:
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
                    if part.get("cost", 0):
                        result["error"] = (
                            "non-zero cost reported; stopped (free model only)"
                        )
                        break
            else:
                process.wait(timeout=30)
                if process.returncode == 0 and texts:
                    result["status"] = "complete"
                elif time.monotonic() - started >= timeout:
                    result["error"] = "timeout"
                else:
                    result["error"] = f"opencode exited {process.returncode}"
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
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
    args = parser.parse_args(argv)
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        results = list(
            pool.map(
                lambda task: run_task(task, args.mode, args.base, args.timeout),
                args.task,
            )
        )
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if all(r["status"] == "complete" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
