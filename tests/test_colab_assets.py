import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import colab_cache as cache
import colab_remote_job as remote_job
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

    def download_asset(self, asset, path, maximum, hasher=None):
        data = self.saved[asset["name"]]
        if len(data) > maximum:
            raise BundleError("size exceeded")
        path.write_bytes(data)
        if hasher is not None:
            hasher.update(data)

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

    def test_cache_restore_reads_archive_in_place_without_copying(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, persistent = base / "local", base / "drive"
            (root / "uv").mkdir(parents=True)
            (root / "uv/package").write_text("PUBLIC")
            persistent.mkdir()
            cache.save("c" * 24, root, persistent)
            manifest = json.loads((persistent / ("c" * 24 + ".json")).read_text())
            archive = persistent / manifest["archive"]
            before = archive.read_bytes()
            shutil.rmtree(root)
            root.mkdir()
            with patch.object(cache.shutil, "copyfile", side_effect=AssertionError):
                cache.restore("c" * 24, root, persistent)
            self.assertEqual((root / "uv/package").read_text(), "PUBLIC")
            self.assertEqual(archive.read_bytes(), before)
            self.assertEqual(
                sorted(p.name for p in persistent.iterdir()),
                sorted([archive.name, "c" * 24 + ".json"]),
            )

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


class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.script = self.root / "tools/colab_setup.sh"

    def test_setup_timings_are_read_and_namespaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "setup-timings.tsv"
            path.write_text(
                "cache_restore\t0.5\nos_deps\t2.25\nbroken\tnot-a-number\nno-tab\n"
            )
            self.assertEqual(
                remote_job.read_setup_timings(path),
                {"setup.cache_restore": 0.5, "setup.os_deps": 2.25},
            )
            self.assertEqual(
                remote_job.read_setup_timings(Path(temporary) / "missing.tsv"), {}
            )

    def test_setup_script_has_valid_bash_syntax(self):
        subprocess.run(["bash", "-n", self.script], check=True)

    def test_setup_save_is_skipped_only_when_nothing_new_was_fetched(self):
        source = self.script.read_text()
        match = re.search(
            r"^clipforge_should_save\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(match)
        for prepare_only, fetched, expected in (
            ("0", "0", 1),
            ("1", "0", 0),
            ("0", "1", 0),
            ("1", "1", 0),
        ):
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    match.group(0) + f"clipforge_should_save {prepare_only} {fetched}",
                ]
            )
            self.assertEqual(result.returncode, expected)


class RemotePipelineTests(unittest.TestCase):
    def test_audio_url_uses_cuda_job_and_preserves_result_publication(self):
        url = (
            "https://kick.com/test-channel/videos/01234567-89ab-cdef-0123-456789abcdef"
        )
        for audio_url in (url, ""):
            with (
                self.subTest(audio_url=bool(audio_url)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                work = root / "job"
                output = work / "out"
                payload = {
                    "mode": "pipeline",
                    "request_id": "a" * 32,
                    "source_ref": "b" * 40,
                    "input_tag": "clipforge-input-" + "a" * 32,
                    "workflow_run": "123",
                    "github_token": "test-only-github",
                }
                if audio_url:
                    payload["audio_url"] = audio_url
                else:
                    payload["gemini_api_key"] = "test-only-gemini"
                status = "topics_allocated" if audio_url else "complete"

                def command(arguments, environment, name, **kwargs):
                    if name == "pipeline":
                        (output / "manifest.json").write_text(
                            json.dumps({"status": status})
                        )
                        (output / "request-receipt.json").write_text("{}")

                bundles = Mock()
                bundles.restore.return_value = True
                with (
                    patch.object(remote_job, "ROOT", root),
                    patch.object(remote_job, "WORK", work),
                    patch.object(remote_job, "PERSISTENT", root / "cache"),
                    patch.object(remote_job, "Path") as paths,
                    patch.object(
                        remote_job,
                        "configuration",
                        return_value=("cache-key", root, root),
                    ),
                    patch.object(remote_job, "ReleaseBundles", return_value=bundles),
                    patch.object(remote_job, "gpu_info", return_value={}),
                    patch.object(remote_job, "host_info", return_value={}),
                    patch.object(remote_job, "read_setup_timings", return_value={}),
                    patch.object(remote_job.os, "umask"),
                    patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-inherited"}),
                    patch.object(remote_job, "command", side_effect=command) as run,
                ):
                    paths.return_value.read_text.return_value = json.dumps(payload)
                    remote_job.main()
                calls = {call.args[2]: call for call in run.call_args_list}
                invocation, environment, _ = calls["pipeline"].args
                self.assertIn("source tools/colab_cuda_env.sh", invocation[2])
                self.assertIn("source tools/colab_cache_env.sh", invocation[2])
                self.assertIn(str(root / ".venv/bin/python"), invocation)
                self.assertEqual(invocation[-2:], ["--output", str(output)])
                if audio_url:
                    self.assertEqual(set(calls), {"setup", "pipeline"})
                    self.assertIn("tools/audio_url_job.py", invocation)
                    self.assertNotIn("tools/run_request.py", invocation)
                    for option, value in (
                        ("--url", url),
                        ("--request-id", payload["request_id"]),
                        ("--source-ref", payload["source_ref"]),
                        ("--workflow-run", "123"),
                    ):
                        self.assertEqual(
                            invocation[invocation.index(option) + 1], value
                        )
                    self.assertNotIn("GEMINI_API_KEY", environment)
                    self.assertNotIn("GH_TOKEN", environment)
                    self.assertNotIn("GITHUB_TOKEN", environment)
                else:
                    self.assertEqual(set(calls), {"setup", "prepare", "pipeline"})
                    self.assertIn("tools/run_request.py", invocation)
                    self.assertIn("run", invocation)
                    self.assertEqual(environment["GEMINI_API_KEY"], "test-only-gemini")
                    self.assertIn("prepare", calls["prepare"].args[0])
                self.assertEqual(environment["CLIPFORGE_REQUIRE_CUDA"], "1")
                bundles.publish.assert_called_once()
                published = bundles.publish.call_args.args
                self.assertEqual(published[:2], (payload["input_tag"], output))
                self.assertEqual(published[3:], ("result", payload["request_id"]))
                report = json.loads((output / "gpu-receipt.json").read_text())
                self.assertEqual(report["status"], status)
                self.assertIn("pipeline", report["timings"])
                self.assertEqual(report["public_cache_saved"], "skipped_restored")


if __name__ == "__main__":
    unittest.main()


class StreamedRestoreTests(unittest.TestCase):
    def publish_many_chunks(self, root, kind, tag, identity):
        import colab_bundle

        source = root / "source"
        source.mkdir()
        # Incompressible bytes, so even the gzip bundle spans several small chunks.
        (source / "blob.bin").write_bytes(os.urandom(40000))
        (source / "small.txt").write_text("ordered")
        bundles = MemoryBundles()
        work = root / "upload"
        work.mkdir()
        with patch.object(colab_bundle.pack, "__defaults__", (4096,)):
            bundles.publish(tag, source, work, kind, identity)
        return bundles, source

    def test_many_chunks_download_in_parallel_and_extract_in_order(self):
        for kind, tag, identity in (
            ("assets", "clipforge-cache-" + "a" * 24, "key"),
            ("result", "clipforge-input-" + "a" * 32, "request"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bundles, source = self.publish_many_chunks(root, kind, tag, identity)
                self.assertGreater(
                    sum(name.endswith(".part") for name in bundles.saved), 3
                )
                bundles.restore(tag, root / "restored", kind, identity, required=True)
                for name in ("blob.bin", "small.txt"):
                    self.assertEqual(
                        (root / "restored" / name).read_bytes(),
                        (source / name).read_bytes(),
                    )
                self.assertEqual(sorted(p.name for p in root.glob("*.part")), [])

    def test_a_corrupted_middle_chunk_commits_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tag = "clipforge-input-" + "b" * 32
            bundles, _ = self.publish_many_chunks(root, "result", tag, "request")
            names = [n for n in bundles.saved if n.endswith(".part")]
            middle = names[len(names) // 2]
            bundles.saved[middle] = b"X" + bundles.saved[middle][1:]
            with self.assertRaisesRegex(BundleError, "checksum"):
                bundles.restore(tag, root / "out", "result", "request", required=True)
            self.assertFalse((root / "out").exists())
