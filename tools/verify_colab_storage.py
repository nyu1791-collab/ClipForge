#!/usr/bin/env python3
"""Live test of private release transfers with synthetic data and exact cleanup."""

import json
import os
from pathlib import Path
import tempfile
import urllib.error
import uuid

from colab_bundle import REPOSITORY, ReleaseBundles


def main():
    key = uuid.uuid4().hex[:24]
    tag = "clipforge-cache-" + key
    bundles = ReleaseBundles(os.environ.get("GH_TOKEN", ""))
    try:
        with tempfile.TemporaryDirectory(prefix="clipforge-live-storage-") as temporary:
            root = Path(temporary)
            source, work, output = (
                root / "public-assets",
                root / "upload",
                root / "restored",
            )
            source.mkdir()
            work.mkdir()
            content = bytes(range(256)) * 1000
            (source / "synthetic.bin").write_bytes(content)
            (source / "metadata.json").write_text('{"synthetic":true}')
            bundles.publish(tag, source, work, "assets", key)
            bundles.restore(tag, output, "assets", key, required=True)
            if (output / "synthetic.bin").read_bytes() != content:
                raise RuntimeError("Live transfer content differs")
            before = {a["id"] for a in bundles.release(tag)["assets"]}
            bundles.publish(tag, output, work, "assets", key)
            after = {a["id"] for a in bundles.release(tag)["assets"]}
            if before != after:
                raise RuntimeError("Cache hit unexpectedly uploaded new assets")
            print(
                json.dumps(
                    {
                        "live_private_storage_transfer": "passed",
                        "sha256_and_content_checked": True,
                        "cache_hit_skipped_upload": True,
                        "real_user_media_used": False,
                    }
                )
            )
    finally:
        release = bundles.release(tag)
        if release:
            bundles.json_request(
                "DELETE",
                f"https://api.github.com/repos/{REPOSITORY}/releases/{release['id']}",
            )
            bundles.json_request(
                "DELETE",
                f"https://api.github.com/repos/{REPOSITORY}/git/refs/tags/{tag}",
            )
        if bundles.release(tag) is not None:
            raise RuntimeError("Temporary private release cleanup was not confirmed")
        print("Live storage fixture release and tag cleaned up.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        status = (
            f" HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else ""
        )
        print("Private storage verification failed: " + type(error).__name__ + status)
        raise SystemExit(1) from None
