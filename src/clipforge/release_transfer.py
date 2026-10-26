"""Private stage handoff without post-upload readback audits (standard library only)."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote
import zipfile

from .github_cli import run
from .storage import PipelineError

REPO = "nyu1791-collab/ai-model-workers"
MAX_BYTES = 1900 * 1024**2
MAX_FILES = 2000


class TransferError(PipelineError):
    pass


def fail(code):
    raise TransferError(code)


def gh(*args):
    return run(
        *args,
        timeout=600,
        error_type=TransferError,
        message="RESULT_UPLOAD_FAILED",
        include_http_status=True,
    )


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def member(name):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or path.as_posix() != name
        or name == "."
    ):
        fail("RESULT_STRUCTURE_INVALID")
    return path


def inventory(root, *, include_hashes=True):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        fail("RESULT_STRUCTURE_INVALID")
    files = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            fail("RESULT_STRUCTURE_INVALID")
        if path.is_file():
            name = path.relative_to(root).as_posix()
            member(name)
            size = path.stat().st_size
            total += size
            if total > MAX_BYTES or len(files) >= MAX_FILES:
                fail("RESULT_STRUCTURE_INVALID")
            files[name] = {"bytes": size}
            if include_hashes:
                files[name]["sha256"] = sha(path)
    if not files:
        fail("RESULT_STRUCTURE_INVALID")
    return files


def validate_identity(root, run_id, name, source_ref=None, *, audit_hashes=True):
    match = re.fullmatch(r"clipforge-(analysis|result)-([a-f0-9]{32})", name)
    if not re.fullmatch(r"[0-9]{1,20}", str(run_id)) or not match:
        fail("RESULT_STRUCTURE_INVALID")
    receipt = json.loads((root / "request-receipt.json").read_text())
    if (
        receipt.get("request_id") != match[2]
        or str(receipt.get("workflow_run")) != str(run_id)
        or not re.fullmatch(r"[a-f0-9]{40}", str(receipt.get("source_ref")))
        or (source_ref is not None and receipt.get("source_ref") != source_ref)
    ):
        fail("RESULT_STRUCTURE_INVALID")
    hashes = receipt.get("sha256")
    if not isinstance(hashes, dict) or not hashes:
        fail("RESULT_STRUCTURE_INVALID")
    for filename, expected in hashes.items():
        member(filename)
        path = root / filename
        if not path.is_file() or path.is_symlink():
            fail("RESULT_STRUCTURE_INVALID")
        if audit_hashes and sha(path) != expected:
            fail("RESULT_SHA_MISMATCH")
    return receipt


def verify_manifest(root, manifest):
    if manifest.get("schema") != 1 or inventory(root) != manifest.get("files"):
        fail("RESULT_SHA_MISMATCH")
    validate_identity(
        root, manifest["run_id"], manifest["name"], manifest["source_ref"]
    )


def safe_extract(archive, destination):
    """The archive root is the stage root; nested parent directories are never guessed."""
    if destination.exists() or any(
        p.is_symlink() for p in [destination, *destination.parents]
    ):
        fail("RESULT_STRUCTURE_INVALID")
    destination.mkdir(mode=0o700)
    seen = set()
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            member(info.filename)
            mode = info.external_attr >> 16
            if (
                info.filename in seen
                or info.is_dir()
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or info.flag_bits & 1
            ):
                fail("RESULT_STRUCTURE_INVALID")
            seen.add(info.filename)
            total += info.file_size
            if total > MAX_BYTES or len(seen) > MAX_FILES:
                fail("RESULT_STRUCTURE_INVALID")
            target = destination / info.filename
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with bundle.open(info) as source, target.open("xb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            target.chmod(0o600)


def private_repo():
    if gh("api", "repos/" + REPO, "--jq", ".private").strip() != "true":
        fail("RESULT_STRUCTURE_INVALID")


def tag_for(run_id, name):
    if not re.fullmatch(r"[0-9]{1,20}", str(run_id)) or not re.fullmatch(
        r"clipforge-(analysis|result)-[a-f0-9]{32}", name
    ):
        fail("RESULT_STRUCTURE_INVALID")
    return f"{name}-run-{run_id}"


def release(tag):
    # Resolve the saved tag directly instead of walking every historical Release.
    # Only an explicit 404 means absence; auth/network errors are not absence.
    try:
        return json.loads(
            gh("api", f"repos/{REPO}/releases/tags/{quote(tag, safe='')}")
        )
    except TransferError as error:
        if str(error).endswith("(HTTP 404)"):
            return None
        raise


def download(run_id, name, destination, expected=None, *, record=None):
    tag = tag_for(run_id, name)
    record = record if record is not None else release(tag)
    if record is None:
        fail("RESULT_NOT_FOUND")
    assets = record["assets"]
    primary = [a for a in assets if a["name"] in {"result.zip", "transfer.json"}]
    if (
        len(primary) != 2
        or {a["name"] for a in primary} != {"result.zip", "transfer.json"}
        or any(a["size"] > MAX_BYTES for a in primary)
    ):
        fail("RESULT_STRUCTURE_INVALID")
    destination = Path(destination).absolute()
    if destination.exists() or any(
        p.is_symlink() for p in [destination, *destination.parents]
    ):
        fail("RESULT_STRUCTURE_INVALID")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        work = Path(temporary)
        gh(
            "release",
            "download",
            tag,
            "--repo",
            REPO,
            "--pattern",
            "result.zip",
            "--pattern",
            "transfer.json",
            "--dir",
            str(work),
        )
        manifest = json.loads((work / "transfer.json").read_text())
        if (
            manifest.get("run_id") != str(run_id)
            or manifest.get("name") != name
            or (expected is not None and manifest != expected)
        ):
            fail("RESULT_STRUCTURE_INVALID")
        unpacked = work / "unpacked"
        safe_extract(work / "result.zip", unpacked)
        validate_identity(
            unpacked, run_id, name, manifest["source_ref"], audit_hashes=False
        )
        if destination.exists() or destination.is_symlink():
            fail("RESULT_STRUCTURE_INVALID")
        unpacked.rename(destination)
    return manifest


def download_stage(run_id, name, destination, artifact_runner=None):
    """Prefer the immutable Release; only absence permits legacy Artifact retrieval."""
    record = release(tag_for(run_id, name))
    if record is not None:
        return download(run_id, name, destination, record=record)
    runner = artifact_runner or gh
    runner(
        "run",
        "download",
        str(run_id),
        "--repo",
        REPO,
        "--name",
        name,
        "--dir",
        str(destination),
    )
    return None


def read_transfer(run_id, name):
    """Read only the small transfer record, never the media archive."""
    tag = tag_for(run_id, name)
    with tempfile.TemporaryDirectory() as temporary:
        gh(
            "release",
            "download",
            tag,
            "--repo",
            REPO,
            "--pattern",
            "transfer.json",
            "--dir",
            temporary,
        )
        record = json.loads((Path(temporary) / "transfer.json").read_text())
    if record.get("run_id") != str(run_id) or record.get("name") != name:
        fail("RESULT_STRUCTURE_INVALID")
    return record


def publish(directory, name):
    private_repo()
    root = Path(directory)
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    tag = tag_for(run_id, name)
    receipt = validate_identity(
        root,
        run_id,
        name,
        os.environ.get("CLIPFORGE_SOURCE_REF") or os.environ.get("SOURCE_REF"),
        audit_hashes=False,
    )
    files = inventory(root, include_hashes=False)
    manifest = {
        "schema": 2,
        "run_id": run_id,
        "name": name,
        "source_ref": receipt["source_ref"],
        "files": files,
        "producer_sha256": receipt.get("sha256", {}),
        "request_receipt": receipt,
        "stage_manifest": json.loads((root / "manifest.json").read_text())
        if (root / "manifest.json").is_file()
        else {},
        "gpu_receipt": json.loads((root / "gpu-receipt.json").read_text())
        if (root / "gpu-receipt.json").is_file()
        else None,
        "post_upload_audit": "not_run",
    }
    if release(tag) is not None:
        existing = read_transfer(run_id, name)
        if existing != manifest:
            fail("RESULT_STRUCTURE_INVALID")
        print("RESULT_REUSED", flush=True)
        return
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        archive = work / "result.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for filename in files:
                info = zipfile.ZipInfo(filename)
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with (
                    (root / filename).open("rb") as source,
                    bundle.open(info, "w", force_zip64=True) as out,
                ):
                    shutil.copyfileobj(source, out, 1024 * 1024)
        if archive.stat().st_size > MAX_BYTES:
            fail("RESULT_STRUCTURE_INVALID")
        (work / "transfer.json").write_text(json.dumps(manifest, sort_keys=True))
        print("RESULT_PACKAGED", flush=True)
        gh(
            "release",
            "create",
            tag,
            str(archive),
            str(work / "transfer.json"),
            "--repo",
            REPO,
            "--title",
            tag,
            "--notes",
            "Private stage transfer",
            "--latest=false",
        )
        # Standalone references are convenient, but never block the primary archive.
        references = []
        try:
            for path in root.glob("clips/clip-*/contact-sheet.jpg"):
                reference = work / (path.parent.name + "-contact-sheet.jpg")
                shutil.copyfile(path, reference)
                references.append(str(reference))
            if references:
                run(
                    "release",
                    "upload",
                    tag,
                    *references,
                    "--repo",
                    REPO,
                    timeout=60,
                    error_type=TransferError,
                    message="REFERENCE_IMAGE_UPLOAD_FAILED",
                )
                print("REFERENCE_IMAGES_UPLOADED; no readback audit", flush=True)
        except (OSError, TransferError, subprocess.TimeoutExpired):
            print("REFERENCE_IMAGE_UPLOAD_SKIPPED; video archive preserved", flush=True)
    print("RESULT_UPLOADED", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("directory", type=Path)
    pub.add_argument("name")
    get = sub.add_parser("download")
    get.add_argument("run_id")
    get.add_argument("name")
    get.add_argument("destination", type=Path)
    get.add_argument("--expected-manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "publish":
            publish(args.directory, args.name)
        else:
            expected = (
                json.loads(args.expected_manifest.read_text())
                if args.expected_manifest
                else None
            )
            download(args.run_id, args.name, args.destination, expected)
    except TransferError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ):
        print(
            "Private result transfer failed; remote result is not verified",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
