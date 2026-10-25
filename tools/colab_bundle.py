#!/usr/bin/env python3
"""Stream private result bundles in bounded chunks, verifying every chunk."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "nyu1791-collab/ai-model-workers"
CHUNK_BYTES = 512 * 1024 * 1024
DOWNLOAD_WORKERS = 4
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


def check_member_name(name):
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise BundleError("Unexpected bundle member name")


def safe_tar_filter(root):
    root = root.resolve()

    def choose(info):
        check_member_name(info.name)
        if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
            raise BundleError("Unsupported bundle entry")
        if info.issym():
            source = root / info.name
            target = source.resolve()
            if not target.is_relative_to(root):
                raise BundleError("Bundle link leaves its root")
            check_member_name(str(target.relative_to(root)))
            info.linkname = os.path.relpath(target, source.parent)
        return info

    return choose


class _ChunkWriter:
    """Write tar bytes directly into upload parts, without a second disk copy."""

    def __init__(self, work, chunk_bytes):
        self.work, self.chunk_bytes = work, chunk_bytes
        self.total = self.size = 0
        self.handle = None
        self.chunks = []

    def write(self, data):
        count = len(data)
        if self.total + count > MAX_BYTES:
            raise BundleError("Bundle exceeds the transfer limit")
        self.total += count
        remaining = memoryview(data)
        while remaining:
            if self.handle is None:
                self.handle = (self.work / "next.part").open("wb")
                self.sha = hashlib.sha256()
                self.size = 0
            length = min(len(remaining), self.chunk_bytes - self.size)
            self.handle.write(remaining[:length])
            self.sha.update(remaining[:length])
            self.size += length
            remaining = remaining[length:]
            if self.size == self.chunk_bytes:
                self.finish()
        return count

    def finish(self):
        if self.handle is None:
            return
        self.handle.close()
        self.handle = None
        checksum = self.sha.hexdigest()
        name = f"cf-result-{checksum}.part"
        (self.work / "next.part").replace(self.work / name)
        self.chunks.append({"name": name, "sha256": checksum, "bytes": self.size})


def pack(root, work, identity, chunk_bytes=CHUNK_BYTES):
    root, work = Path(root).resolve(), Path(work).resolve()
    if work.is_relative_to(root) or chunk_bytes <= 0:
        raise BundleError(
            "Bundle staging must be outside its source with positive chunks"
        )
    work.mkdir(parents=True, exist_ok=True)
    stream = _ChunkWriter(work, chunk_bytes)
    try:
        with tarfile.open(fileobj=stream, mode="w|") as bundle:
            for path in sorted(root.iterdir()):
                bundle.add(
                    path,
                    arcname=str(path.relative_to(root)),
                    filter=safe_tar_filter(root),
                )
        stream.finish()
    finally:
        if stream.handle is not None:
            stream.handle.close()
    manifest = {
        "schema": 1,
        "kind": "result",
        "identity": identity,
        "bytes": stream.total,
        "chunks": stream.chunks,
    }
    path = work / "cf-result-manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def check_manifest(data, identity):
    if (data.get("schema"), data.get("kind"), data.get("identity")) != (
        1,
        "result",
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
        if chunk.get("name") != f"cf-result-{sha}.part":
            raise BundleError("Invalid chunk name")
        size = chunk.get("bytes")
        if type(size) is not int or not 0 < size <= CHUNK_BYTES:
            raise BundleError("Invalid chunk size")
        total += size
    if total != data.get("bytes") or total > MAX_BYTES:
        raise BundleError("Invalid bundle size")


class ChunkStream(io.RawIOBase):
    """Read verified chunk files in order as one stream, deleting each once it is read.

    Each item is a future that returns a chunk's path after its checksum matched, so no byte
    is read before its whole chunk is verified.
    """

    def __init__(self, futures, on_consumed):
        self.futures = futures
        self.on_consumed = on_consumed
        self.current = None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.current is None:
                if not self.futures:
                    return 0
                path = self.futures[0].result()
                self.current = (path, path.open("rb"))
            count = self.current[1].readinto(buffer)
            if count:
                return count
            path, handle = self.current
            handle.close()
            path.unlink()
            self.current = None
            self.futures.popleft()
            self.on_consumed()

    def finish(self):
        """Read past tar's end padding so every chunk is verified and removed."""
        while self.read(1024 * 1024):
            pass


class ReleaseBundles:
    def __init__(self, token):
        if not token:
            raise BundleError("Private release authentication is missing")
        self.token = token
        self._local = threading.local()
        info = self.json_request("GET", f"https://api.github.com/repos/{REPOSITORY}")
        if not info.get("private"):
            raise BundleError("Bundle repository must remain private")

    @property
    def opener(self):
        # Chunks download on several threads; each thread keeps its own opener.
        if not hasattr(self._local, "opener"):
            self._local.opener = urllib.request.build_opener(SafeRedirect())
        return self._local.opener

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

    def release(self, tag):
        if not re.fullmatch(r"clipforge-input-[a-f0-9]{32}", tag):
            raise BundleError("Unexpected private release tag")
        url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}"
        return self.json_request("GET", url, allow_missing=True)

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
            self.open(
                "POST",
                url,
                data=iter(lambda: data.read(1024 * 1024), b""),
                size=path.stat().st_size,
            ) as response,
        ):
            json.load(response)

    def publish(self, tag, directory, work, identity):
        release = self.release(tag)
        if not release:
            raise BundleError("Private input release is missing")
        manifest = pack(directory, work, identity)
        data = json.loads(manifest.read_text())
        for name in sorted({c["name"] for c in data["chunks"]}):
            self.upload(release, manifest.parent / name)
        self.upload(release, manifest)

    def download_asset(self, asset, path, maximum, hasher=None):
        """Save one asset; hasher, when given, sees every byte as it is written."""
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
                if hasher is not None:
                    hasher.update(data)
        if count != asset["size"]:
            raise BundleError("Private asset transfer was incomplete")

    def download_chunk(self, asset, path, chunk):
        """Download and verify one chunk, hashing while it arrives rather than re-reading it."""
        sha = hashlib.sha256()
        self.download_asset(asset, path, chunk["bytes"], sha)
        if path.stat().st_size != chunk["bytes"] or sha.hexdigest() != chunk["sha256"]:
            raise BundleError("Private bundle checksum mismatch")
        return path

    def restore(self, tag, destination, identity, required=False):
        release = self.release(tag)
        assets = {a["name"]: a for a in release["assets"]} if release else {}
        manifest = assets.get("cf-result-manifest.json")
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
            check_manifest(data, identity)
            for chunk in data["chunks"]:
                if chunk["name"] not in assets:
                    raise BundleError("A private bundle chunk is missing")
            unpacked = temporary / "unpacked"
            unpacked.mkdir()
            # Chunks download side by side and are extracted in order as each one is
            # verified, streamed straight into tar: no joined copy of the bundle is written.
            # Refill only after consumption deletes a chunk, bounding disk use even on slow tar.
            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                pending = deque()
                chunks = iter(enumerate(data["chunks"]))

                def submit_next():
                    item = next(chunks, None)
                    if item is None:
                        return
                    i, chunk = item
                    pending.append(
                        pool.submit(
                            self.download_chunk,
                            assets[chunk["name"]],
                            temporary / f"{i}.part",
                            chunk,
                        )
                    )

                try:
                    for _ in range(DOWNLOAD_WORKERS + 1):
                        submit_next()
                    stream = ChunkStream(pending, submit_next)
                    with tarfile.open(fileobj=stream, mode="r|") as bundle:
                        count = total = 0
                        for member in bundle:
                            check_member_name(member.name)
                            if member.islnk():
                                check_member_name(member.linkname)
                            elif member.issym():
                                # data_filter checks the resolved target, including symlinks
                                # already extracted; the whitelist also forbids metadata links.
                                parent = (unpacked / member.name).parent
                                target = parent / member.linkname
                                target = target.resolve()
                                if not target.is_relative_to(unpacked.resolve()):
                                    raise BundleError("Bundle link leaves its root")
                                check_member_name(
                                    str(target.relative_to(unpacked.resolve()))
                                )
                            count += 1
                            total += member.size
                            if count > 200000 or total > MAX_BYTES:
                                raise BundleError(
                                    "Expanded bundle exceeds its size limit"
                                )
                            bundle.extract(member, unpacked, filter="data")
                    stream.finish()
                finally:
                    for future in pending:
                        future.cancel()
            if destination.exists():
                destination.rmdir()
            unpacked.rename(destination)
        return True
