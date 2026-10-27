from concurrent.futures import Future
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import colab_cache as cache
import colab_remote_job as remote_job
from colab_bundle import BundleError, ReleaseBundles, SafeRedirect, check_manifest, pack

TAG = "clipforge-input-" + "a" * 32


class MemoryBundles(ReleaseBundles):
    def __init__(self):
        self.saved = {}
        self.metadata = {"id": 1, "assets": []}
        self.uploads = 0

    def release(self, tag):
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


class BundleTests(unittest.TestCase):
    def test_private_result_roundtrip_keeps_files_and_inside_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "clips").mkdir(parents=True)
            (source / "clips/video.mp4").write_bytes(b"synthetic video" * 5000)
            (source / "manifest.json").write_text('{"status":"complete"}')
            (source / "alias.mp4").symlink_to("clips/video.mp4")
            bundles = MemoryBundles()
            work = root / "upload"
            work.mkdir()
            bundles.publish(TAG, source, work, "request")
            bundles.restore(TAG, root / "restored", "request", required=True)
            for name in ("clips/video.mp4", "manifest.json", "alias.mp4"):
                self.assertEqual(
                    (root / "restored" / name).read_bytes(),
                    (source / name).read_bytes(),
                )

    def test_missing_result_is_reported_only_when_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundles = MemoryBundles()
            self.assertFalse(bundles.restore(TAG, Path(temporary) / "out", "request"))
            with self.assertRaisesRegex(BundleError, "missing"):
                bundles.restore(TAG, Path(temporary) / "out", "request", required=True)

    def test_transfer_rejects_corrupted_chunk_without_committing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "output.mp4").write_bytes(b"video")
            bundles = MemoryBundles()
            work = root / "upload"
            work.mkdir()
            bundles.publish(TAG, source, work, "request")
            chunk = next(x for x in bundles.saved if x.endswith(".part"))
            bundles.saved[chunk] = b"X" + bundles.saved[chunk][1:]
            with self.assertRaisesRegex(BundleError, "checksum"):
                bundles.restore(TAG, root / "out", "request", required=True)
            self.assertFalse((root / "out").exists())

    def test_chunks_are_bounded_and_manifest_identity_is_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "binary").write_bytes(bytes(range(256)) * 30)
            path = pack(source, root / "work", "request", chunk_bytes=4096)
            manifest = json.loads(path.read_text())
            self.assertGreater(len(manifest["chunks"]), 1)
            self.assertTrue(all(c["bytes"] <= 4096 for c in manifest["chunks"]))
            check_manifest(manifest, "request")
            with self.assertRaises(BundleError):
                check_manifest(manifest, "other-request")
            manifest["chunks"][0]["name"] = "../../credentials"
            with self.assertRaises(BundleError):
                check_manifest(manifest, "request")

    def test_outside_symlink_is_never_packaged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (root / "secret").write_text("DO_NOT_ARCHIVE")
            (source / "escape").symlink_to(root / "secret")
            with self.assertRaises(BundleError):
                pack(source, root / "work", "request")

    def test_only_temporary_input_releases_are_used(self):
        bundles = ReleaseBundles.__new__(ReleaseBundles)
        for tag in ("clipforge-cache-" + "a" * 24, "other", "clipforge-input-x"):
            with self.subTest(tag=tag), self.assertRaises(BundleError):
                bundles.release(tag)

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


class StreamedRestoreTests(unittest.TestCase):
    def publish_many_chunks(self, root, identity):
        import colab_bundle

        source = root / "source"
        source.mkdir()
        (source / "blob.bin").write_bytes(os.urandom(40000))
        (source / "small.txt").write_text("ordered")
        bundles = MemoryBundles()
        work = root / "upload"
        work.mkdir()
        with patch.object(colab_bundle.pack, "__defaults__", (4096,)):
            bundles.publish(TAG, source, work, identity)
        return bundles, source

    def test_many_chunks_download_in_parallel_and_extract_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundles, source = self.publish_many_chunks(root, "request")
            self.assertGreater(sum(name.endswith(".part") for name in bundles.saved), 3)
            bundles.restore(TAG, root / "restored", "request", required=True)
            for name in ("blob.bin", "small.txt"):
                self.assertEqual(
                    (root / "restored" / name).read_bytes(),
                    (source / name).read_bytes(),
                )
            self.assertEqual(sorted(p.name for p in root.glob("*.part")), [])

    def test_a_corrupted_middle_chunk_commits_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundles, _ = self.publish_many_chunks(root, "request")
            names = [n for n in bundles.saved if n.endswith(".part")]
            middle = names[len(names) // 2]
            bundles.saved[middle] = b"X" + bundles.saved[middle][1:]
            with self.assertRaisesRegex(BundleError, "checksum"):
                bundles.restore(TAG, root / "out", "request", required=True)
            self.assertFalse((root / "out").exists())

    def test_download_window_refills_only_after_a_chunk_is_deleted(self):
        import colab_bundle

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundles, source = self.publish_many_chunks(root, "request")
            paths = []
            outstanding = []

            def submit(function, asset, path, chunk):
                # Downloads finish immediately while extraction makes no progress.
                # A new submission must wait until an earlier file has been deleted.
                self.assertLess(
                    sum(p.exists() for p in paths), colab_bundle.DOWNLOAD_WORKERS + 1
                )
                future = Future()
                future.set_result(function(asset, path, chunk))
                paths.append(path)
                outstanding.append(sum(p.exists() for p in paths))
                return future

            with patch.object(colab_bundle, "ThreadPoolExecutor") as executor:
                executor.return_value.__enter__.return_value.submit.side_effect = submit
                bundles.restore(TAG, root / "restored", "request", required=True)
            self.assertGreater(len(paths), colab_bundle.DOWNLOAD_WORKERS + 1)
            self.assertEqual(max(outstanding), colab_bundle.DOWNLOAD_WORKERS + 1)
            self.assertFalse(any(p.exists() for p in paths))
            self.assertEqual(
                (root / "restored/blob.bin").read_bytes(),
                (source / "blob.bin").read_bytes(),
            )


class CacheConfigurationTests(unittest.TestCase):
    def test_removed_cache_commands_are_rejected_before_configuration(self):
        for command in ("restore", "save", "runner", "github-env"):
            with (
                self.subTest(command=command),
                patch.object(sys, "argv", ["colab_cache.py", command]),
                patch.object(cache, "configuration") as configuration,
                self.assertRaises(SystemExit) as error,
            ):
                cache.main()
            self.assertEqual(error.exception.code, 2)
            configuration.assert_not_called()

    def test_cache_directory_changes_with_lock_and_os(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"CLIPFORGE_LOCAL_CACHE": temporary}),
        ):
            first, root = cache.configuration()
            self.assertEqual(root, Path(temporary) / first)
            self.assertEqual(
                set(cache.environment(root)),
                {
                    "CLIPFORGE_CACHE_ROOT",
                    "UV_CACHE_DIR",
                    "UV_PYTHON_INSTALL_DIR",
                    "PIP_CACHE_DIR",
                },
            )
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
            path.write_text("os_deps\t2.25\nbroken\tnot-a-number\nno-tab\n")
            self.assertEqual(
                remote_job.read_setup_timings(path), {"setup.os_deps": 2.25}
            )
            self.assertEqual(
                remote_job.read_setup_timings(Path(temporary) / "missing.tsv"), {}
            )

    def test_setup_scripts_have_valid_bash_syntax(self):
        for name in (
            "colab_setup.sh",
            "colab_os_deps.sh",
            "colab_cache_env.sh",
            "ensure_node.sh",
        ):
            subprocess.run(["bash", "-n", self.root / "tools" / name], check=True)

    def test_uv_sync_tries_the_local_cache_first_and_needs_no_cuda(self):
        source = self.script.read_text()
        self.assertNotIn("cuda", source)
        self.assertNotIn("whisper", source.lower())
        block = source.split("uv_sync_started=$(date +%s.%N)\n", 1)[1].split(
            "clipforge_record_timing uv_sync", 1
        )[0]
        for fail_offline in ("0", "1"):
            with self.subTest(fail_offline=fail_offline):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'set -e; uv() { printf "%s\\n" "$*"; [[ $* != *--offline* || $FAIL_OFFLINE != 1 ]]; }; '
                        + block,
                    ],
                    env={**os.environ, "FAIL_OFFLINE": fail_offline},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                calls = [
                    line.split()
                    for line in result.stdout.splitlines()
                    if line.startswith("sync ")
                ]
                self.assertIn("--offline", calls[0])
                self.assertEqual(len(calls), 2 if fail_offline == "1" else 1)
                for arguments in calls:
                    self.assertIn("--locked", arguments)
                    self.assertIn("media-fetch", arguments)

    def test_os_packages_keep_render_dependencies(self):
        source = (self.root / "tools/colab_os_deps.sh").read_text()
        block = source.split("packages=(", 1)[1].split("for package in", 1)[0]
        result = subprocess.run(
            ["bash", "-c", "packages=(" + block + 'printf "%s\\n" "${packages[@]}"'],
            capture_output=True,
            text=True,
            check=True,
        )
        packages = result.stdout.splitlines()
        self.assertTrue(
            {"ffmpeg", "fontconfig", "fonts-noto-cjk", "git", "gh"}.issubset(packages)
        )


class FakePartBundles:
    """The part's private release: its request and audio, and the published result."""

    def __init__(self, request, audio, *, size=None):
        self.files = {"request.json": json.dumps(request).encode(), "audio.wav": audio}
        self.size = size
        self.published = []

    def release(self, tag):
        return {
            "assets": [
                {
                    "name": name,
                    "size": self.size
                    if name == "audio.wav" and self.size
                    else len(data),
                }
                for name, data in self.files.items()
            ]
        }

    def download_asset(self, asset, path, maximum):
        path.write_bytes(self.files[asset["name"]])

    def publish(self, tag, directory, work, identity):
        self.published.append(
            (tag, identity, sorted(p.name for p in Path(directory).iterdir()))
        )


class RemoteAsrTests(unittest.TestCase):
    def run_part(self, change=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        work = root / "job"
        audio = b"RIFF synthetic part audio"
        request = {
            "request_id": "a" * 32,
            "source_ref": "b" * 40,
            "audio_sha256": hashlib.sha256(audio).hexdigest(),
            "audio_bytes": len(audio),
            "language": "ja",
            "asr_part": {"index": 1, "count": 4, "start": 10.0, "end": 20.0},
        }
        size = None
        if change == "identity":
            request["request_id"] = "c" * 32
        elif change == "checksum":
            request["audio_sha256"] = "0" * 64
        elif change == "size":
            size = len(audio) + 1
        payload = {
            "mode": "asr",
            "request_id": "a" * 32,
            "source_ref": "b" * 40,
            "input_tag": TAG,
            "workflow_run": "123",
            "github_token": "test-only-github",
            "accelerator": "L4",
        }
        payload_path = root / "payload.json"
        payload_path.write_text(json.dumps(payload))
        bundles = FakePartBundles(request, audio, size=size)
        events = []

        class Prefetch:
            def __init__(self, arguments, **kwargs):
                events.append(("prefetch", arguments, kwargs["env"]))

            def wait(self, timeout):
                events.append(("prefetch_wait", None, None))
                return 0

            def poll(self):
                return 0

        def command(arguments, environment, name, **kwargs):
            events.append((name, arguments, environment))
            output = Path(arguments[arguments.index("--output") + 1])
            output.write_text('{"language": "ja", "segments": []}')

        real_path = Path
        with (
            patch.object(remote_job, "ROOT", root),
            patch.object(remote_job, "WORK", work),
            patch.object(
                remote_job,
                "Path",
                side_effect=lambda *a: (
                    payload_path
                    if a == ("/content/.clipforge-job.json",)
                    else real_path(*a)
                ),
            ),
            patch.object(remote_job, "ReleaseBundles", return_value=bundles),
            patch.object(remote_job, "gpu_info", return_value={"devices": []}),
            patch.object(remote_job, "host_info", return_value={}),
            patch.object(remote_job.subprocess, "Popen", Prefetch),
            patch.object(remote_job, "command", side_effect=command),
            patch.object(remote_job.os, "umask"),
            patch.dict(os.environ, {"GH_TOKEN": "inherited-token"}),
        ):
            failure = None
            try:
                remote_job.main()
            except RuntimeError as error:
                failure = error
        return work / "out", bundles, events, request, failure

    def test_part_is_recognised_after_its_input_and_published_with_checksums(self):
        output, bundles, events, request, failure = self.run_part()
        self.assertIsNone(failure)
        self.assertEqual(
            [event[0] for event in events], ["prefetch", "prefetch_wait", "recognise"]
        )
        self.assertEqual(events[0][1][1:], ["-m", "clipforge.asr", "prefetch"])
        self.assertEqual(events[2][1][1:4], ["-m", "clipforge.asr", "recognise"])
        for name, _, environment in (events[0], events[2]):
            self.assertNotIn("GH_TOKEN", environment)
            # The runtime's own Python reads the source tree, never a project venv.
            self.assertTrue(environment["PYTHONPATH"].endswith("/src"))
            self.assertFalse(any("VIRTUAL_ENV" in key for key in environment))
        self.assertEqual(
            json.loads((output / "manifest.json").read_text()),
            {
                "status": "asr_part_complete",
                "asr_part": request["asr_part"],
                "audio_sha256": request["audio_sha256"],
            },
        )
        receipt = json.loads((output / "request-receipt.json").read_text())
        self.assertEqual(receipt["status"], "asr_part_complete")
        self.assertEqual(receipt["workflow_run"], "123")
        self.assertEqual(
            set(receipt["sha256"]),
            {"manifest.json", "transcript.json", "gpu-receipt.json"},
        )
        report = json.loads((output / "gpu-receipt.json").read_text())
        self.assertEqual(report["status"], "asr_part_complete")
        self.assertTrue(
            {"input", "prefetch_wait", "recognise"} <= set(report["timings"])
        )
        ((tag, identity, files),) = bundles.published
        self.assertEqual((tag, identity), (TAG, "a" * 32))
        self.assertIn("transcript.json", files)

    def test_mismatched_part_input_is_refused_before_recognition(self):
        for change in ("identity", "checksum", "size"):
            with self.subTest(change=change):
                output, bundles, events, _, failure = self.run_part(change)
                self.assertIsInstance(failure, RuntimeError)
                self.assertNotIn("recognise", [event[0] for event in events])
                report = json.loads((output / "gpu-receipt.json").read_text())
                self.assertEqual(report["error_type"], "RuntimeError")
                self.assertEqual(report["stage"], "private-input")
                # The failure is still published so the control plane can report it.
                self.assertEqual(len(bundles.published), 1)


class RemoteRenderConfigTests(unittest.TestCase):
    def render_arguments(self, config_file):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        work = root / "job"
        stage = {
            "previous_run": "122",
            "previous_request": "c" * 32,
            "review": '{"clips": []}',
            "clip_index": "1",
        }
        if config_file is not None:
            stage["config_file"] = config_file
        payload = {
            "mode": "audio-stage",
            "request_id": "a" * 32,
            "source_ref": "b" * 40,
            "input_tag": TAG,
            "workflow_run": "123",
            "github_token": "test-only-github",
            "accelerator": "NONE",
            "stage": stage,
        }
        captured = []

        def command(arguments, environment, name, **kwargs):
            if name == "render":
                captured.append(arguments)
                (work / "out").mkdir(parents=True, exist_ok=True)
                (work / "out/manifest.json").write_text('{"status": "clip_rendered"}')

        with (
            patch.object(remote_job, "ROOT", root),
            patch.object(remote_job, "WORK", work),
            patch.object(remote_job, "Path") as paths,
            patch.object(remote_job, "ReleaseBundles", return_value=Mock()),
            patch.object(remote_job, "host_info", return_value={}),
            patch.object(remote_job, "gpu_info", side_effect=AssertionError),
            patch.object(remote_job, "read_setup_timings", return_value={}),
            patch.object(remote_job, "publish_result", return_value=True),
            patch.object(remote_job.os, "umask"),
            patch.object(remote_job, "command", side_effect=command),
        ):
            paths.return_value.read_text.return_value = json.dumps(payload)
            try:
                remote_job.main()
            except RuntimeError:
                pass
        return captured

    def test_render_passes_the_requested_configuration_to_the_worker(self):
        (arguments,) = self.render_arguments("config.landscape-chat.example.json")
        self.assertEqual(
            arguments[-2:], ["--config", "config.landscape-chat.example.json"]
        )

    def test_render_without_a_configuration_keeps_the_worker_default(self):
        (arguments,) = self.render_arguments(None)
        self.assertNotIn("--config", arguments)

    def test_render_rejects_a_configuration_outside_the_pattern(self):
        self.assertEqual(self.render_arguments("../config.landscape.example.json"), [])


class LogTailTests(unittest.TestCase):
    def test_only_the_last_ascii_lines_are_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recognise.log"
            path.write_text(
                "\n".join(
                    [f"line {i}" for i in range(30)] + ["認識した本文", "x" * 400]
                )
            )
            tail = remote_job.log_tail(path)
            self.assertEqual(len(tail), 20)
            self.assertNotIn("認識した本文", tail)
            self.assertEqual(tail[-1], "x" * 300)
            self.assertEqual(remote_job.log_tail(Path(temporary) / "missing"), [])

    def test_tail_redacts_signed_urls_and_environment_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prefetch.log"
            path.write_text(
                "download https://example.invalid/path?token=secret\n"
                "token=synthetic-secret-value\n"
            )
            with unittest.mock.patch.dict(
                os.environ, {"GH_TOKEN": "synthetic-secret-value"}
            ):
                tail = remote_job.log_tail(path)
            self.assertNotIn("example.invalid", "\n".join(tail))
            self.assertNotIn("synthetic-secret-value", "\n".join(tail))
