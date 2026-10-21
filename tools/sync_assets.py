#!/usr/bin/env python3
"""Import checksum-pinned assets or video cache from HTTPS, S3, or a mount."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sync(manifest, assets, cache, allow_hosts, mount=None, s3_endpoint=None):
    items = manifest["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("require 1-100 items")
    receipts, total = [], 0
    for item in items:
        relative = Path(item["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in str(relative)
            or len(relative.parts) < 2
        ):
            raise ValueError("invalid asset path")
        video = item.get("kind", "asset") == "video"
        if relative.parts[0] not in (
            {"video"} if video else {"fonts", "se", "bgm", "overlays"}
        ):
            raise ValueError("unsupported asset category")
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or not item.get("license"):
            raise ValueError("sha256 and license are required")
        limit = int(item.get("max_bytes", 32 * 1024**2))
        if not 1 <= limit <= (2 * 1024**3 if video else 32 * 1024**2):
            raise ValueError("invalid item byte budget")
        base = Path(cache if video else assets).resolve()
        base.mkdir(parents=True, exist_ok=True)
        target = base / relative
        if not target.resolve().is_relative_to(base) or any(
            p.is_symlink() for p in [target, *target.parents] if p != base.parent
        ):
            raise ValueError("symlink/escaping destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("existing file differs; use a new name")
            receipts.append(
                {
                    "path": str(relative),
                    "status": "already_present",
                    "sha256": item["sha256"],
                }
            )
            continue
        source = item["source"]
        parsed = urlsplit(source)
        handle = None
        if parsed.scheme == "https":
            if (
                parsed.hostname not in allow_hosts
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
            ):
                raise ValueError("HTTPS host must be explicitly allowed")
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None
            if (address is not None and not address.is_global) or parsed.hostname in {
                "localhost",
                "metadata.google.internal",
            }:
                raise ValueError("private destination is unsupported")
            handle = build_opener(NoRedirect()).open(source, timeout=30)
        elif parsed.scheme == "s3":
            import boto3

            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "s3 URLs must not contain credentials or query parameters"
                )

            # Standard provider credentials remain in the environment/profile.
            if s3_endpoint:
                endpoint = urlsplit(s3_endpoint)
                if (
                    endpoint.scheme != "https"
                    or endpoint.hostname not in allow_hosts
                    or endpoint.username
                    or endpoint.password
                ):
                    raise ValueError("S3 endpoint must be allowed HTTPS")
            handle = boto3.client("s3", endpoint_url=s3_endpoint).get_object(
                Bucket=parsed.netloc, Key=parsed.path.lstrip("/")
            )["Body"]
        elif not parsed.scheme and mount:
            root = Path(mount).resolve(strict=True)
            local = root / source
            if (
                local.is_symlink()
                or not local.resolve().is_relative_to(root)
                or not local.is_file()
            ):
                raise ValueError("mount source escapes root")
            handle = local.open("rb")
        else:
            raise ValueError("source must be HTTPS, s3://, or a mounted relative path")
        temporary = None
        try:
            sha, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
                temporary = Path(out.name)
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > limit or total > 2 * 1024**3:
                        raise ValueError("sync budget exceeded")
                    sha.update(chunk)
                    out.write(chunk)
            if sha.hexdigest() != item["sha256"]:
                raise ValueError("asset checksum mismatch")
            os.link(temporary, target)
            receipts.append(
                {
                    "path": str(relative),
                    "status": "imported",
                    "sha256": sha.hexdigest(),
                    "bytes": size,
                    "license": item["license"],
                }
            )
        finally:
            handle.close()
            if temporary:
                temporary.unlink(missing_ok=True)
    return receipts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifest", type=Path)
    p.add_argument("--assets", type=Path, default=Path("assets"))
    p.add_argument("--cache", type=Path, default=Path("workspace/cache"))
    p.add_argument("--allow-host", action="append", default=[])
    p.add_argument("--mount", type=Path)
    p.add_argument("--s3-endpoint")
    a = p.parse_args()
    try:
        print(
            json.dumps(
                {
                    "items": sync(
                        json.loads(a.manifest.read_text()),
                        a.assets,
                        a.cache,
                        a.allow_host,
                        a.mount,
                        a.s3_endpoint,
                    )
                },
                ensure_ascii=False,
            )
        )
    except Exception as e:
        print(json.dumps({"status": "sync_failed", "error_type": type(e).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
