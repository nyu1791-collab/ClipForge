"""Shared `gh` command runner for the ClipForge tools and worker.

Callers choose their own timeout, failure exception type and failure message.
Failure messages never include stderr: gh may echo signed URLs or tokens there.
Only the HTTP status code can be added, and only when the caller asks for it.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile

DEFAULT_TIMEOUT = 120


def run(
    *args,
    body=None,
    timeout=DEFAULT_TIMEOUT,
    error_type=RuntimeError,
    message="GitHub operation failed",
    include_command=False,
    include_http_status=False,
):
    """Run `gh` with args, send body on stdin, and return stdout.

    On a non-zero exit, raise error_type(message). With include_command, the
    first argument (the gh subcommand) is appended after ": ". With
    include_http_status, " (HTTP nnn)" is appended when stderr names a status.
    A subprocess.TimeoutExpired from timeout is not converted.
    """
    result = subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        try:
            if _release_download(args, timeout):
                return ""
        except subprocess.TimeoutExpired:
            raise
        except Exception:
            raise error_type("private result release verification failed") from None
        text = message
        if include_command:
            text += ": " + args[0]
        if include_http_status:
            status = re.search(r"HTTP ([0-9]{3})\b", result.stderr)
            if status:
                text += " (HTTP " + status[1] + ")"
        raise error_type(text)
    return result.stdout


PRIVATE_REPO = "nyu1791-collab/ai-model-workers"
RESULT_NAME = re.compile(r"clipforge-(result|analysis)-([a-f0-9]{32})")
MAX_RESULT_BYTES = 2 * 1024**3


def _sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024**2), b""):
            value.update(chunk)
    return value.hexdigest()


def _extract_result(archive, destination):
    """Extract only bounded regular files into a new directory."""
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        names = set()
        if (
            len(members) > 500
            or sum(row.file_size for row in members) > MAX_RESULT_BYTES
        ):
            raise RuntimeError("private result exceeds the extraction budget")
        for row in members:
            name = row.filename.rstrip("/")
            path = PurePosixPath(name)
            kind = stat.S_IFMT(row.external_attr >> 16)
            if (
                not name
                or name in names
                or path.is_absolute()
                or path.as_posix() != name
                or ".." in path.parts
                or "\\" in name
                or row.flag_bits & 1
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
            ):
                raise RuntimeError("unsafe private result archive")
            names.add(name)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("private result destination already exists")
        destination.mkdir(parents=True, mode=0o700)
        source.extractall(destination)


def _release_download(args, timeout):
    """Read the explicitly published result when no Actions artifact is available."""
    if (
        len(args) != 9
        or args[:2] != ("run", "download")
        or args[3] != "--repo"
        or args[4] != PRIVATE_REPO
        or args[5] != "--name"
        or args[7] != "--dir"
        or not re.fullmatch(r"[0-9]{1,20}", str(args[2]))
        or not RESULT_NAME.fullmatch(args[6])
    ):
        return False
    destination = Path(args[8])
    # gh may create an empty directory before discovering that no artifact exists.
    if destination.is_symlink() or (
        destination.exists() and any(destination.iterdir())
    ):
        raise RuntimeError("refusing to replace a nonempty private result directory")
    if run("api", "repos/" + PRIVATE_REPO, "--jq", ".private").strip() != "true":
        raise RuntimeError("result transfer requires the private worker repository")
    name = args[6]
    tag = "clipforge-output-" + name.removeprefix("clipforge-")
    details = json.loads(
        run("release", "view", tag, "--repo", PRIVATE_REPO, "--json", "body,assets")
    )
    note = json.loads(details["body"])
    assets = details["assets"]
    if (
        note.get("schema") != 1
        or note.get("artifact_name") != name
        or str(note.get("workflow_run")) != str(args[2])
        or not re.fullmatch(r"[a-f0-9]{40}", note.get("source_ref", ""))
        or not re.fullmatch(r"[a-f0-9]{64}", note.get("sha256", ""))
        or type(note.get("bytes")) is not int
        or not 0 < note["bytes"] <= MAX_RESULT_BYTES
        or len(assets) != 1
        or assets[0].get("name") != "result.zip"
        or assets[0].get("size") != note["bytes"]
    ):
        raise RuntimeError("private result release identity mismatch")
    with tempfile.TemporaryDirectory(prefix="clipforge-output-") as temporary:
        archive = Path(temporary) / "result.zip"
        run(
            "release",
            "download",
            tag,
            "--repo",
            PRIVATE_REPO,
            "--pattern",
            "result.zip",
            "--dir",
            temporary,
            timeout=timeout,
        )
        if (
            archive.stat().st_size != note["bytes"]
            or _sha256(archive) != note["sha256"]
        ):
            raise RuntimeError("private result archive checksum mismatch")
        if destination.exists():
            destination.rmdir()
        _extract_result(archive, destination)
    return True


def publish_result(directory, name):
    """Publish one immutable, checksummed temporary result in the private worker."""
    directory = Path(directory)
    match = RESULT_NAME.fullmatch(name)
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not match or not re.fullmatch(r"[0-9]{1,20}", run_id):
        raise RuntimeError("invalid private result identity")
    if run("api", "repos/" + PRIVATE_REPO, "--jq", ".private").strip() != "true":
        raise RuntimeError("result transfer requires the private worker repository")
    receipt = json.loads((directory / "request-receipt.json").read_text())
    if (
        receipt.get("request_id") != match[2]
        or str(receipt.get("workflow_run")) != run_id
    ):
        raise RuntimeError("private result receipt does not match the publishing run")
    source_ref = receipt.get("source_ref", "")
    if not re.fullmatch(r"[a-f0-9]{40}", source_ref):
        raise RuntimeError("private result source commit is missing")
    files = sorted(path for path in directory.rglob("*") if not path.is_dir())
    if (
        directory.is_symlink()
        or not 1 <= len(files) <= 500
        or any(
            path.is_symlink() or not path.is_file()
            for path in directory.rglob("*")
            if not path.is_dir() or path.is_symlink()
        )
    ):
        raise RuntimeError("private result must contain regular files")
    if sum(path.stat().st_size for path in files) > MAX_RESULT_BYTES:
        raise RuntimeError("private result exceeds the transfer budget")
    with tempfile.TemporaryDirectory(prefix="clipforge-publish-") as temporary:
        archive = Path(temporary) / "result.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
            for path in files:
                output.write(path, path.relative_to(directory).as_posix())
        if archive.stat().st_size > MAX_RESULT_BYTES:
            raise RuntimeError("private result archive exceeds the transfer budget")
        note = {
            "schema": 1,
            "artifact_name": name,
            "workflow_run": run_id,
            "source_ref": source_ref,
            "bytes": archive.stat().st_size,
            "sha256": _sha256(archive),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }
        notes = Path(temporary) / "notes.json"
        notes.write_text(json.dumps(note))
        tag = "clipforge-output-" + name.removeprefix("clipforge-")
        run(
            "release",
            "create",
            tag,
            str(archive),
            "--repo",
            PRIVATE_REPO,
            "--target",
            "main",
            "--title",
            tag,
            "--latest=false",
            "--notes-file",
            str(notes),
            timeout=600,
        )
    print("Private result transfer published with SHA-256 verification.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("directory", type=Path)
    publish.add_argument("name")
    download = commands.add_parser("download")
    download.add_argument("run_id")
    download.add_argument("name")
    download.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "publish":
        publish_result(args.directory, args.name)
    else:
        run(
            "run",
            "download",
            args.run_id,
            "--repo",
            PRIVATE_REPO,
            "--name",
            args.name,
            "--dir",
            str(args.directory),
            timeout=600,
        )


if __name__ == "__main__":
    main()
