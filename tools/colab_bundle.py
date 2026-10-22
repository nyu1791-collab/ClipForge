#!/usr/bin/env python3
"""Stream private release bundles in bounded chunks, verifying every chunk."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "nyu1791-collab/ai-model-workers"
CHUNK_BYTES = 512 * 1024 * 1024
MAX_BYTES = 24 * 1024**3
CDN_HOSTS = {
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}


class BundleError(RuntimeError):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or (
            new.hostname != old.hostname and new.hostname not in CDN_HOSTS
        ):
            raise BundleError("Unexpected asset redirect")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and new.hostname != old.hostname:
            redirected.remove_header("Authorization")
        return redirected


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def safe_tar_filter(root):
    root = root.resolve()

    def choose(info):
        if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
            raise BundleError("Unsupported bundle entry")
        if info.issym():
            source = root / info.name
            target = source.resolve()
            if not target.is_relative_to(root):
                raise BundleError("Bundle link leaves its root")
            info.linkname = os.path.relpath(target, source.parent)
        return info

    return choose


def tree_id(root):
    entries = []
    for path in sorted(Path(root).rglob("*")):
        if path.name.endswith(".lock") or (path.is_dir() and not path.is_symlink()):
            continue
        stat = path.lstat()
        detail = (
            os.readlink(path)
            if path.is_symlink()
            else (digest(path) if stat.st_size <= 4096 else "")
        )
        entries.append((str(path.relative_to(root)), stat.st_size, detail))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def pack(root, work, kind, identity, chunk_bytes=CHUNK_BYTES):
    if kind not in {"assets", "result"}:
        raise BundleError("Unexpected bundle kind")
    root, work = Path(root).resolve(), Path(work).resolve()
    if work.is_relative_to(root):
        raise BundleError("Bundle staging must be outside its source")
    work.mkdir(parents=True, exist_ok=True)
    archive = work / "bundle.tar"
    with tarfile.open(archive, "w") as bundle:
        for path in sorted(root.iterdir()):
            bundle.add(path, arcname=path.name, filter=safe_tar_filter(root))
    if archive.stat().st_size > MAX_BYTES:
        raise BundleError("Bundle exceeds the transfer limit")
    chunks = []
    with archive.open("rb") as source:
        while True:
            temporary = work / "next.part"
            size = 0
            sha = hashlib.sha256()
            with temporary.open("wb") as out:
                while size < chunk_bytes:
                    data = source.read(min(1024 * 1024, chunk_bytes - size))
                    if not data:
                        break
                    out.write(data)
                    sha.update(data)
                    size += len(data)
            if not size:
                temporary.unlink()
                break
            name = f"cf-{kind}-{sha.hexdigest()}.part"
            temporary.replace(work / name)
            chunks.append({"name": name, "sha256": sha.hexdigest(), "bytes": size})
    archive.unlink()
    manifest = {
        "schema": 1,
        "kind": kind,
        "identity": identity,
        "bytes": sum(c["bytes"] for c in chunks),
        "chunks": chunks,
    }
    path = work / f"cf-{kind}-manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def check_manifest(data, kind, identity):
    if (data.get("schema"), data.get("kind"), data.get("identity")) != (
        1,
        kind,
        identity,
    ):
        raise BundleError("Bundle identity mismatch")
    chunks = data.get("chunks")
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= 1024:
        raise BundleError("Invalid bundle chunks")
    total = 0
    for chunk in chunks:
        sha = chunk.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise BundleError("Invalid chunk checksum")
        if chunk.get("name") != f"cf-{kind}-{sha}.part":
            raise BundleError("Invalid chunk name")
        size = chunk.get("bytes")
        if type(size) is not int or not 0 < size <= CHUNK_BYTES:
            raise BundleError("Invalid chunk size")
        total += size
    if total != data.get("bytes") or total > MAX_BYTES:
        raise BundleError("Invalid bundle size")


class ReleaseBundles:
    def __init__(self, token):
        if not token:
            raise BundleError("Private release authentication is missing")
        self.token = token
        self.opener = urllib.request.build_opener(SafeRedirect())
        info = self.json_request("GET", f"https://api.github.com/repos/{REPOSITORY}")
        if not info.get("private"):
            raise BundleError("Bundle repository must remain private")

    def open(self, method, url, *, data=None, size=None, binary=False):
        host = urllib.parse.urlsplit(url).hostname
        if host not in {"api.github.com", "uploads.github.com"}:
            raise BundleError("Unexpected GitHub API host")
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/octet-stream"
            if binary
            else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ClipForge-Colab",
        }
        if data is not None:
            headers["Content-Type"] = (
                "application/octet-stream" if size is not None else "application/json"
            )
        if size is not None:
            headers["Content-Length"] = str(size)
        request = urllib.request.Request(url, method=method, headers=headers, data=data)
        return self.opener.open(request, timeout=180)

    def json_request(self, method, url, body=None, allow_missing=False):
        try:
            data = json.dumps(body).encode() if body is not None else None
            with self.open(method, url, data=data) as response:
                value = response.read(16 * 1024 * 1024 + 1)
                if len(value) > 16 * 1024 * 1024:
                    raise BundleError("GitHub metadata exceeds its size limit")
                return json.loads(value) if value else {}
        except urllib.error.HTTPError as error:
            if error.code == 404 and allow_missing:
                return None
            raise BundleError(
                f"Private GitHub operation failed (HTTP {error.code})"
            ) from None

    def release(self, tag, create=False):
        if not re.fullmatch(
            r"clipforge-(?:input-[a-f0-9]{32}|cache-[a-f0-9]{24})", tag
        ):
            raise BundleError("Unexpected private release tag")
        url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}"
        release = self.json_request("GET", url, allow_missing=True)
        if release is None and create:
            if not tag.startswith("clipforge-cache-"):
                raise BundleError("Only public-asset cache releases may be created")
            release = self.json_request(
                "POST",
                f"https://api.github.com/repos/{REPOSITORY}/releases",
                {
                    "tag_name": tag,
                    "target_commitish": "main",
                    "name": tag,
                    "prerelease": True,
                    "body": "Public ClipForge runtime assets only. No credentials or job data.",
                },
            )
        return release

    def upload(self, release, path):
        name = path.name
        # Chunk names are immutable content hashes; the manifest is published last.
        existing = next((a for a in release["assets"] if a["name"] == name), None)
        if existing and name.endswith(".part"):
            if existing["size"] != path.stat().st_size:
                raise BundleError("Existing cache chunk has the wrong size")
            return
        if existing:
            self.json_request("DELETE", existing["url"])
        url = f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release['id']}/assets?"
        url += urllib.parse.urlencode({"name": name})
        with (
            path.open("rb") as data,
            self.open("POST", url, data=data, size=path.stat().st_size) as response,
        ):
            json.load(response)

    def publish(self, tag, directory, work, kind, identity):
        release = self.release(tag, create=kind == "assets")
        if not release:
            raise BundleError("Private input release is missing")
        inventory = tree_id(directory) if kind == "assets" else None
        existing = next(
            (a for a in release["assets"] if a["name"] == f"cf-{kind}-manifest.json"),
            None,
        )
        if existing and kind == "assets":
            previous = Path(work) / "previous.json"
            self.download_asset(existing, previous, 1024 * 1024)
            old = json.loads(previous.read_text())
            check_manifest(old, kind, identity)
            if old.get("tree_id") == inventory:
                print("Public asset cache unchanged; upload skipped.")
                return
        manifest = pack(directory, work, kind, identity)
        data = json.loads(manifest.read_text())
        if inventory is not None:
            data["tree_id"] = inventory
            manifest.write_text(json.dumps(data))
        for name in sorted({c["name"] for c in data["chunks"]}):
            self.upload(release, manifest.parent / name)
        self.upload(release, manifest)
        if kind == "assets":
            keep = {c["name"] for c in data["chunks"]}
            for asset in release["assets"]:
                if (
                    re.fullmatch(r"cf-assets-[a-f0-9]{64}\.part", asset["name"])
                    and asset["name"] not in keep
                ):
                    self.json_request("DELETE", asset["url"])

    def download_asset(self, asset, path, maximum):
        if not 0 < asset["size"] <= maximum:
            raise BundleError("Private asset size exceeds its limit")
        count = 0
        with (
            self.open("GET", asset["url"], binary=True) as response,
            path.open("xb") as out,
        ):
            while data := response.read(1024 * 1024):
                count += len(data)
                if count > maximum:
                    raise BundleError("Private asset exceeds its transfer limit")
                out.write(data)
        if count != asset["size"]:
            raise BundleError("Private asset transfer was incomplete")

    def restore(self, tag, destination, kind, identity, required=False):
        release = self.release(tag)
        assets = {a["name"]: a for a in release["assets"]} if release else {}
        manifest = assets.get(f"cf-{kind}-manifest.json")
        if not manifest:
            if required:
                raise BundleError("Private result bundle is missing")
            return False
        destination = Path(destination)
        if destination.exists() and any(destination.iterdir()):
            raise BundleError("Bundle destination must be empty")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            temporary = Path(temporary)
            record = temporary / "manifest.json"
            self.download_asset(manifest, record, 1024 * 1024)
            data = json.loads(record.read_text())
            check_manifest(data, kind, identity)
            archive = temporary / "bundle.tar"
            with archive.open("wb") as out:
                for i, chunk in enumerate(data["chunks"]):
                    if chunk["name"] not in assets:
                        raise BundleError("A private bundle chunk is missing")
                    path = temporary / f"{i}.part"
                    self.download_asset(assets[chunk["name"]], path, chunk["bytes"])
                    if (
                        path.stat().st_size != chunk["bytes"]
                        or digest(path) != chunk["sha256"]
                    ):
                        raise BundleError("Private bundle checksum mismatch")
                    with path.open("rb") as source:
                        shutil.copyfileobj(source, out, 1024 * 1024)
                    path.unlink()
            unpacked = temporary / "unpacked"
            unpacked.mkdir()
            with tarfile.open(archive) as bundle:
                count = total = 0
                for member in bundle:
                    count += 1
                    total += member.size
                    if count > 200000 or total > MAX_BYTES:
                        raise BundleError("Expanded bundle exceeds its size limit")
                    bundle.extract(member, unpacked, filter="data")
            if destination.exists():
                destination.rmdir()
            unpacked.rename(destination)
        return True
