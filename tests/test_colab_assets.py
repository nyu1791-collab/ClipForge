import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import colab_cache as cache
from colab_bundle import BundleError, ReleaseBundles, SafeRedirect, check_manifest, pack


class MemoryBundles(ReleaseBundles):
    def __init__(self):
        self.saved = {}
        self.metadata = {"id": 1, "assets": []}
        self.uploads = 0

    def release(self, tag, create=False):
        return self.metadata

    def upload(self, release, path):
        self.uploads += 1
        self.saved[path.name] = path.read_bytes()
        release["assets"][:] = [x for x in release["assets"] if x["name"] != path.name]
        release["assets"].append(
            {"name": path.name, "size": path.stat().st_size, "url": path.name}
        )

    def download_asset(self, asset, path, maximum):
        data = self.saved[asset["name"]]
        if len(data) > maximum:
            raise BundleError("size exceeded")
        path.write_bytes(data)

    def json_request(self, method, url, **kwargs):
        if method != "DELETE":
            raise AssertionError("unexpected metadata mutation")
        self.saved.pop(url, None)
        self.metadata["assets"][:] = [
            a for a in self.metadata["assets"] if a["url"] != url
        ]


class BundleTests(unittest.TestCase):
    def test_private_transfer_roundtrip_and_cache_hit_skips_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "model.bin").write_bytes(b"public model bytes" * 5000)
            (source / "config.json").write_text('{"precision":"float16"}')
            (source / "model-alias").symlink_to("model.bin")
            bundles = MemoryBundles()
            work = root / "upload"
            work.mkdir()
            bundles.publish(
                "clipforge-cache-" + "a" * 24, source, work, "assets", "key"
            )
            uploads = bundles.uploads
            bundles.restore(
                "clipforge-cache-" + "a" * 24,
                root / "restored",
                "assets",
                "key",
                required=True,
            )
            self.assertEqual(
                (root / "restored/model.bin").read_bytes(),
                (source / "model.bin").read_bytes(),
            )
            self.assertEqual(
                (root / "restored/model-alias").read_bytes(),
                (source / "model.bin").read_bytes(),
            )
            bundles.publish(
                "clipforge-cache-" + "a" * 24, root / "restored", work, "assets", "key"
            )
            self.assertEqual(bundles.uploads, uploads)

    def test_transfer_rejects_corrupted_chunk_without_committing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "output.mp4").write_bytes(b"video")
            bundles = MemoryBundles()
            work = root / "upload"
            work.mkdir()
            bundles.publish(
                "clipforge-input-" + "a" * 32, source, work, "result", "request"
            )
            chunk = next(x for x in bundles.saved if x.endswith(".part"))
            bundles.saved[chunk] = b"X" + bundles.saved[chunk][1:]
            with self.assertRaisesRegex(BundleError, "checksum"):
                bundles.restore(
                    "clipforge-input-" + "a" * 32,
                    root / "out",
                    "result",
                    "request",
                    required=True,
                )
            self.assertFalse((root / "out").exists())

    def test_chunks_are_bounded_and_manifest_identity_is_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "binary").write_bytes(bytes(range(256)) * 30)
            path = pack(source, root / "work", "result", "request", chunk_bytes=4096)
            manifest = json.loads(path.read_text())
            self.assertGreater(len(manifest["chunks"]), 1)
            self.assertTrue(all(c["bytes"] <= 4096 for c in manifest["chunks"]))
            check_manifest(manifest, "result", "request")
            with self.assertRaises(BundleError):
                check_manifest(manifest, "result", "other-request")
            manifest["chunks"][0]["name"] = "../../credentials"
            with self.assertRaises(BundleError):
                check_manifest(manifest, "result", "request")

    def test_outside_symlink_is_never_packaged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (root / "secret").write_text("DO_NOT_ARCHIVE")
            (source / "escape").symlink_to(root / "secret")
            with self.assertRaises(BundleError):
                pack(source, root / "work", "assets", "key")

    def test_download_redirect_drops_github_auth_and_rejects_other_hosts(self):
        redirect = SafeRedirect()
        request = urllib.request.Request(
            "https://api.github.com/repos/x/assets/1",
            headers={"Authorization": "Bearer test-only"},
        )
        new = redirect.redirect_request(
            request,
            None,
            302,
            "found",
            {},
            "https://release-assets.githubusercontent.com/file",
        )
        self.assertIsNone(new.get_header("Authorization"))
        with self.assertRaises(BundleError):
            redirect.redirect_request(
                request, None, 302, "found", {}, "https://example.com/file"
            )
        with self.assertRaises(BundleError):
            redirect.redirect_request(
                request, None, 302, "found", {}, "http://api.github.com/file"
            )


class DependencyCacheTests(unittest.TestCase):
    def test_cache_restore_keeps_public_packages_and_excludes_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "local"
            (root / "uv/archive").mkdir(parents=True)
            (root / "uv/archive/package").write_text("PUBLIC_WHEEL")
            (root / "uv/alias").symlink_to(root / "uv/archive/package")
            (root / "runner-credentials").write_text("PRIVATE_SENTINEL")
            persistent = base / "drive"
            persistent.mkdir()
            cache.save("a" * 24, root, persistent)
            manifest = json.loads((persistent / ("a" * 24 + ".json")).read_text())
            with tarfile.open(persistent / manifest["archive"]) as bundle:
                self.assertTrue(
                    all(m.name.split("/")[0] in cache.PARTS for m in bundle)
                )
            shutil.rmtree(root)
            root.mkdir()
            cache.restore("a" * 24, root, persistent)
            self.assertEqual((root / "uv/alias").read_text(), "PUBLIC_WHEEL")
            self.assertFalse((root / "runner-credentials").exists())

    def test_corrupted_dependency_snapshot_is_not_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, persistent = base / "local", base / "drive"
            (root / "python").mkdir(parents=True)
            persistent.mkdir()
            (root / "python/python").write_bytes(b"public interpreter")
            cache.save("b" * 24, root, persistent)
            manifest = json.loads((persistent / ("b" * 24 + ".json")).read_text())
            (persistent / manifest["archive"]).write_bytes(b"broken archive")
            shutil.rmtree(root)
            root.mkdir()
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                cache.restore("b" * 24, root, persistent)
            self.assertFalse((root / "python").exists())

    def test_cache_configuration_changes_with_lock_and_os(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"CLIPFORGE_LOCAL_CACHE": temporary, "CLIPFORGE_PERSISTENT_CACHE": ""},
            ),
        ):
            first = cache.configuration()[0]
            with patch.object(
                cache.platform,
                "freedesktop_os_release",
                return_value={"ID": "other", "VERSION_ID": "1"},
            ):
                self.assertNotEqual(first, cache.configuration()[0])
            original = cache.sha256
            with patch.object(
                cache,
                "sha256",
                side_effect=lambda p: (
                    "new-lock" if p.name == "uv.lock" else original(p)
                ),
            ):
                self.assertNotEqual(first, cache.configuration()[0])


if __name__ == "__main__":
    unittest.main()
