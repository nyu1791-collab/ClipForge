#!/usr/bin/env python3
"""Prepare a public HEAD copy; inference requires an explicitly selected model.

--check and --prepare never invoke a model or inspect authentication storage.
The prepared copy is a working directory, not an operating-system sandbox.
"""

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "opencode.json",
    ".opencode/commands/clipforge-review.md",
    ".opencode/commands/clipforge-fix.md",
    ".opencode/commands/clipforge-verify.md",
    "tools/opencode_harness.py",
    "docs/OPENCODE_HARNESS.md",
    "harness/check_pipeline.py",
    "harness/lint_captions.py",
    "tests/test_opencode_harness.py",
    "tests/test_quality_harness.py",
    "pyproject.toml",
    "uv.lock",
)
BLOCKED = "gpt-6.1-sol"
MARKER = ".clipforge-harness.json"


def checked(args, cwd=ROOT, **kwargs):
    return subprocess.run(args, cwd=cwd, check=True, **kwargs)


def validate(root):
    for name in FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing or linked harness file: {name}")
        if name.endswith(".md") and name.startswith(".opencode/"):
            header = path.read_text().split("---", 2)[1]
            if "model:" in header or "agent: clipforge" not in header:
                raise ValueError("commands must use clipforge without a default model")
    config = json.loads((root / "opencode.json").read_text())

    def no_model(value):
        if isinstance(value, dict):
            if "model" in value or "small_model" in value:
                raise ValueError("harness must not select a default model")
            for child in value.values():
                no_model(child)
        elif isinstance(value, list):
            for child in value:
                no_model(child)

    no_model(config)
    if config.get("default_agent") != "clipforge" or config.get("share") != "disabled":
        raise ValueError("unexpected agent or sharing setting")
    if BLOCKED not in config["provider"]["opencode"].get("blacklist", []):
        raise ValueError("stopped model must remain blocked")
    permissions = config["agent"]["clipforge"]["permission"]
    for name in ("*", "external_directory", "task", "webfetch", "websearch"):
        if permissions.get(name) != "deny":
            raise ValueError(f"unsafe permission: {name}")
    return config


def cli_path(value):
    candidate = value or shutil.which("opencode")
    if not candidate:
        candidate = "/workspace/.tools/opencode/node_modules/.bin/opencode"
    path = Path(candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("OpenCode CLI unavailable; pass --cli PATH")
    return str(path)


def prepare(root=ROOT, parent=None):
    validate(root)
    head = checked(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    archive = checked(["git", "archive", head], cwd=root, capture_output=True).stdout
    dest = Path(tempfile.mkdtemp(prefix="clipforge-opencode-", dir=parent)).resolve()
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.issym()
                or member.islnk()
                or path.parts[0] in {"workspace", ".git", ".venv"}
            ):
                raise ValueError("unsafe path in public HEAD")
        bundle.extractall(dest, filter="data")
    git = ["git", "-c", "core.hooksPath=/dev/null"]
    checked(git + ["init", "-q"], cwd=dest)
    checked(git + ["add", "."], cwd=dest)
    checked(
        git
        + [
            "-c",
            "user.name=ClipForge harness",
            "-c",
            "user.email=harness@localhost",
            "commit",
            "-qm",
            "Public source " + head,
        ],
        cwd=dest,
    )
    for name in FILES:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    (dest / MARKER).write_text(
        json.dumps({"source_head": head, "project": str(dest)}, indent=2) + "\n"
    )
    with (dest / ".git/info/exclude").open("a") as out:
        out.write("\n" + MARKER + "\n")
    return dest


def launch_command(cli, project, model, command, task):
    if not model or "/" not in model or model.rsplit("/", 1)[-1].lower() == BLOCKED:
        raise ValueError("an explicitly selected, permitted provider/model is required")
    if command not in ("clipforge-review", "clipforge-fix", "clipforge-verify"):
        raise ValueError("unknown ClipForge command")
    return [
        cli,
        "run",
        "--pure",
        "--dir",
        str(project),
        "--agent",
        "clipforge",
        "--model",
        model,
        "--command",
        command,
        "--",
        task,
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument(
        "--start",
        action="store_true",
        help="calls selected model; explicit user authorization required",
    )
    parser.add_argument("--cli")
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--command", default="clipforge-review")
    parser.add_argument("--task", default="")
    args = parser.parse_args(argv)
    try:
        if args.model and not args.start:
            raise ValueError("--model is only accepted with --start")
        if args.start and (not args.model or not args.project or not args.task):
            raise ValueError(
                "--start requires --model, --project and --task; nothing was started"
            )
        validate(ROOT)
        cli = cli_path(args.cli)
        if args.check:
            version = checked(
                [cli, "--version"], capture_output=True, text=True
            ).stdout.strip()
            print(
                json.dumps(
                    {
                        "configuration": "static checks passed",
                        "cli_version": version,
                        "model_requests": 0,
                        "resolved_config_checked": False,
                    }
                )
            )
        elif args.prepare:
            print(prepare(parent=args.parent))
        else:
            project = args.project.resolve()
            receipt = json.loads((project / MARKER).read_text())
            if receipt["project"] != str(project) or project == ROOT:
                raise ValueError("--project must be an intact prepared copy")
            validate(project)
            command = launch_command(cli, project, args.model, args.command, args.task)
            env = os.environ.copy()
            env["PWD"] = str(project)
            env["OPENCODE_CONFIG"] = str(project / "opencode.json")
            verification = [
                "uv",
                "run",
                "--offline",
                "python",
                "harness/check_pipeline.py",
            ]
            checked(verification, cwd=project, env=env)
            checked(command, cwd=project, env=env)
            checked(verification, cwd=project, env=env)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"harness failed: {detail}; no automatic retry\n")


if __name__ == "__main__":
    main()
