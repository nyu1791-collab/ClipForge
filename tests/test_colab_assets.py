from concurrent.futures import Future
import hashlib
import io
import json
import os
from pathlib import Path
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
    def test_profiles_have_distinct_keys_tags_and_model_parts(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"CLIPFORGE_LOCAL_CACHE": temporary, "CLIPFORGE_PERSISTENT_CACHE": ""},
            ),
        ):
            os.environ.pop("CLIPFORGE_CACHE_PROFILE", None)
            profiles = {}
            for cpu_only, profile in (("0", "gpu"), ("1", "cpu")):
                os.environ["CLIPFORGE_CPU_ONLY"] = cpu_only
                key, root, persistent = cache.configuration()
                self.assertIsNone(persistent)
                profiles[profile] = key, cache.release_tag(key)
                self.assertIn(f"cache-{profile}-", cache.release_tag(key))
                self.assertEqual(
                    "models/hub" in cache.release_parts(), profile == "gpu"
                )
                self.assertEqual(
                    cache.environment(root)["HF_HUB_CACHE"],
                    str(root / "models/hub"),
                )
            self.assertNotEqual(profiles["cpu"][0], profiles["gpu"][0])
            self.assertNotEqual(profiles["cpu"][1], profiles["gpu"][1])

    def test_explicit_profile_overrides_cpu_only_for_keys_tags_and_parts(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"CLIPFORGE_LOCAL_CACHE": temporary, "CLIPFORGE_PERSISTENT_CACHE": ""},
            ),
        ):
            for profile in ("cpu", "gpu"):
                with self.subTest(profile=profile):
                    os.environ["CLIPFORGE_CACHE_PROFILE"] = profile
                    keys = []
                    for cpu_only in ("0", "1"):
                        os.environ["CLIPFORGE_CPU_ONLY"] = cpu_only
                        keys.append(cache.configuration()[0])
                        self.assertEqual(cache.profile(), profile)
                        self.assertEqual(
                            cache.release_tag(keys[-1]),
                            f"clipforge-cache-{profile}-{keys[-1]}",
                        )
                        self.assertEqual(
                            cache.release_parts(),
                            cache.PARTS
                            if profile == "cpu"
                            else (*cache.PARTS, "models/hub"),
                        )
                    self.assertEqual(keys[0], keys[1])

    def test_invalid_explicit_profile_is_rejected(self):
        for profile in ("", "CUDA", "other"):
            with (
                self.subTest(profile=profile),
                patch.dict(os.environ, {"CLIPFORGE_CACHE_PROFILE": profile}),
            ):
                with self.assertRaisesRegex(ValueError, "Invalid cache profile"):
                    cache.configuration()

    def test_profiled_release_tags_are_accepted_without_deleting_old_releases(self):
        bundles = object.__new__(ReleaseBundles)
        bundles.json_request = Mock(return_value=None)
        for profile in ("cpu", "gpu"):
            tag = f"clipforge-cache-{profile}-" + "a" * 24
            bundles.release(tag, create=True)
            self.assertEqual(bundles.json_request.call_args.args[2]["tag_name"], tag)
        self.assertFalse(
            any(c.args[0] == "DELETE" for c in bundles.json_request.call_args_list)
        )

    def test_release_restores_selected_assets_once_directly_to_root(self):
        for cpu_only in ("0", "1"):
            with (
                self.subTest(cpu_only=cpu_only),
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(os.environ, {"CLIPFORGE_CPU_ONLY": cpu_only}),
            ):
                base = Path(temporary)
                source, root = base / "source", base / "local"
                (source / "uv/archive").mkdir(parents=True)
                (source / "uv/archive/package").write_text("PUBLIC_WHEEL")
                (source / "uv/alias").symlink_to(source / "uv/archive/package")
                (source / "uv/cache.lock").touch()
                (source / "models/hub").mkdir(parents=True)
                (source / "models/hub/model.bin").write_bytes(b"synthetic model")
                (source / "unrelated").write_text("EXCLUDED")
                work = base / "upload"
                work.mkdir()
                bundles = MemoryBundles()
                tag, parts = cache.release_tag("a" * 24), cache.release_parts()
                bundles.publish(tag, source, work, "assets", "key", parts=parts)
                original_extract = tarfile.TarFile.extract
                destinations = []

                def extract(bundle, member, path, **kwargs):
                    destinations.append(Path(path))
                    self.assertEqual(kwargs["filter"], "data")
                    return original_extract(bundle, member, path, **kwargs)

                with (
                    patch.object(tarfile.TarFile, "extract", new=extract),
                    patch.object(
                        cache, "sha256", side_effect=AssertionError("no inner tar")
                    ),
                ):
                    self.assertTrue(
                        bundles.restore(tag, root, "assets", "key", parts=parts)
                    )
                    cache.mark_restored("key", root)
                self.assertTrue(destinations)
                self.assertEqual(set(destinations), {root})
                self.assertEqual((root / "uv/alias").read_text(), "PUBLIC_WHEEL")
                self.assertEqual(
                    (root / "models/hub/model.bin").exists(), cpu_only == "0"
                )
                self.assertFalse((root / "unrelated").exists())
                self.assertFalse((root / "uv/cache.lock").exists())
                self.assertFalse(list(root.glob("*.tar")))
                self.assertEqual((root / ".restored").read_text(), "key")
                uploads = bundles.uploads
                bundles.publish(tag, root, work, "assets", "key", parts=parts)
                self.assertEqual(bundles.uploads, uploads)

    def test_release_rejects_unsafe_or_out_of_profile_members(self):
        for name, linkname, entry_type in (
            ("../escape", "", tarfile.REGTYPE),
            ("/absolute", "", tarfile.REGTYPE),
            ("uv/../../escape", "", tarfile.REGTYPE),
            ("models/hub/model.bin", "", tarfile.REGTYPE),
            (".restored", "", tarfile.REGTYPE),
            ("uv/link", "../../escape", tarfile.SYMTYPE),
            ("uv/link", "../.restored", tarfile.SYMTYPE),
            ("uv/link", "../escape", tarfile.LNKTYPE),
        ):
            with (
                self.subTest(name=name, linkname=linkname),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
                    member = tarfile.TarInfo(name)
                    member.type = entry_type
                    member.linkname = linkname
                    bundle.addfile(member)
                data = archive.getvalue()
                sha = hashlib.sha256(data).hexdigest()
                chunk_name = f"cf-assets-{sha}.part"
                manifest = {
                    "schema": 1,
                    "kind": "assets",
                    "identity": "key",
                    "bytes": len(data),
                    "chunks": [{"name": chunk_name, "bytes": len(data), "sha256": sha}],
                }
                bundles = MemoryBundles()
                for asset_name, asset_data in (
                    (chunk_name, data),
                    ("cf-assets-manifest.json", json.dumps(manifest).encode()),
                ):
                    bundles.saved[asset_name] = asset_data
                    bundles.metadata["assets"].append(
                        {"name": asset_name, "size": len(asset_data), "url": asset_name}
                    )
                root = base / "local"
                with self.assertRaises((BundleError, tarfile.FilterError)):
                    bundles.restore("tag", root, "assets", "key", parts=cache.PARTS)
                self.assertFalse((root / ".restored").exists())
                self.assertFalse((base / "escape").exists())

    def test_configuration_ignores_removed_drive_cache(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {
                    "CLIPFORGE_LOCAL_CACHE": temporary,
                    "CLIPFORGE_PERSISTENT_CACHE": "/content/drive/removed-cache",
                },
            ),
        ):
            key, root, persistent = cache.configuration()
            self.assertIsNone(persistent)
            self.assertEqual(root, Path(temporary) / key)
            self.assertEqual(
                cache.environment(root)["HF_HUB_CACHE"], str(root / "models/hub")
            )

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

    def profile_block(self):
        return (
            "clipforge_default_profile=gpu\n"
            + self.script.read_text()
            .split("clipforge_default_profile=gpu\n", 1)[1]
            .split("clipforge_timings=", 1)[0]
        )

    def test_setup_resolves_and_validates_profile_before_preparing_assets(self):
        for cpu_only in ("0", "1"):
            for profile in (None, "cpu", "gpu", "", "invalid"):
                with self.subTest(cpu_only=cpu_only, profile=profile):
                    environment = {**os.environ, "CLIPFORGE_CPU_ONLY": cpu_only}
                    environment.pop("CLIPFORGE_CACHE_PROFILE", None)
                    if profile is not None:
                        environment["CLIPFORGE_CACHE_PROFILE"] = profile
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            self.profile_block()
                            + 'printf "%s\\n" "$CLIPFORGE_CACHE_PROFILE"',
                        ],
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    if profile in ("", "invalid"):
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("Invalid cache profile", result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0)
                        self.assertEqual(
                            result.stdout.strip(),
                            profile or ("cpu" if cpu_only == "1" else "gpu"),
                        )

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
        for script in (self.script, self.root / "tools/colab_os_deps.sh"):
            subprocess.run(["bash", "-n", script], check=True)

    def test_uv_sync_uses_profile_extras_and_offline_first(self):
        source = self.script.read_text()
        block = source.split("uv_sync_started=$(date +%s.%N)\n", 1)[1].split(
            "clipforge_record_timing uv_sync", 1
        )[0]
        for cpu_only, profile in (
            ("0", "gpu"),
            ("1", "cpu"),
            ("1", "gpu"),
            ("0", "cpu"),
        ):
            for fail_offline in ("0", "1"):
                for offline in ("0", "1"):
                    with self.subTest(
                        cpu_only=cpu_only,
                        profile=profile,
                        fail_offline=fail_offline,
                        offline=offline,
                    ):
                        result = subprocess.run(
                            [
                                "bash",
                                "-c",
                                'set -e; fetched=0; uv() { printf "%s\\n" "$*"; [[ $* != *--offline* || $FAIL_OFFLINE != 1 ]]; }; '
                                + block,
                            ],
                            env={
                                **os.environ,
                                "CLIPFORGE_CPU_ONLY": cpu_only,
                                "CLIPFORGE_CACHE_PROFILE": profile,
                                "FAIL_OFFLINE": fail_offline,
                                "CLIPFORGE_OFFLINE": offline,
                            },
                            capture_output=True,
                            text=True,
                        )
                        calls = [
                            line.split()
                            for line in result.stdout.splitlines()
                            if line.startswith("sync ")
                        ]
                        self.assertIn("--offline", calls[0])
                        self.assertEqual(
                            result.returncode,
                            int(fail_offline == "1" and offline == "1"),
                        )
                        self.assertEqual(
                            len(calls),
                            2 if fail_offline == "1" and offline == "0" else 1,
                        )
                        for arguments in calls:
                            self.assertEqual("cuda" in arguments, profile == "gpu")
                            self.assertIn("--locked", arguments)
                        if len(calls) == 2:
                            self.assertNotIn("--offline", calls[1])
                        if result.returncode:
                            self.assertIn(
                                "Python dependencies are not fully cached; offline mode forbids downloading.",
                                result.stderr,
                            )

    def test_cuda_check_uses_hardware_and_model_prepare_uses_profile(self):
        source = self.script.read_text()
        blocks = {
            name: source.split(f"{name}_started=$(date +%s.%N)\n", 1)[1].split(
                f"clipforge_record_timing {name}", 1
            )[0]
            for name in ("cuda_check", "model_prepare")
        }
        for cpu_only in ("0", "1"):
            for profile in ("cpu", "gpu"):
                for skip in ("0", "1"):
                    for name, block in blocks.items():
                        with self.subTest(
                            cpu_only=cpu_only, profile=profile, skip=skip, stage=name
                        ):
                            block = block.replace(
                                "source tools/colab_cuda_env.sh", "true"
                            ).replace(".venv/bin/python -", "python_stub -")
                            result = subprocess.run(
                                [
                                    "bash",
                                    "-c",
                                    'set -eu; python_stub() { printf "python-called\\n"; }; '
                                    + block,
                                ],
                                env={
                                    **os.environ,
                                    "CLIPFORGE_CPU_ONLY": cpu_only,
                                    "CLIPFORGE_CACHE_PROFILE": profile,
                                    "CLIPFORGE_SKIP_MODEL_PREPARE": skip,
                                },
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            expected = (
                                cpu_only != "1"
                                if name == "cuda_check"
                                else profile == "gpu" and skip != "1"
                            )
                            self.assertEqual("python-called" in result.stdout, expected)

    def test_os_packages_keep_render_dependencies_and_omit_unused_development_files(
        self,
    ):
        source = (self.root / "tools/colab_os_deps.sh").read_text()
        block = source.split("packages=(", 1)[1].split("for package in", 1)[0]
        for cpu_only in ("0", "1"):
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "packages=(" + block + 'printf "%s\\n" "${packages[@]}"',
                ],
                env={**os.environ, "CLIPFORGE_CPU_ONLY": cpu_only},
                capture_output=True,
                text=True,
                check=True,
            )
            packages = result.stdout.splitlines()
            self.assertNotIn("libicu-dev", packages)
            self.assertEqual("curl" in packages, cpu_only == "0")
            self.assertTrue(
                {"ffmpeg", "fontconfig", "fonts-noto-cjk", "git", "gh"}.issubset(
                    packages
                )
            )

    def test_setup_reports_fetched_assets_for_release_publication(self):
        block = (
            self.script.read_text()
            .split("# Tell the worker that this setup fetched something", 1)[1]
            .split("\n", 1)[1]
        )
        for fetched in ("0", "1"):
            with (
                self.subTest(fetched=fetched),
                tempfile.TemporaryDirectory() as temporary,
            ):
                marker = Path(temporary) / "fetched"
                subprocess.run(
                    ["bash", "-c", block],
                    env={
                        **os.environ,
                        "fetched": fetched,
                        "CLIPFORGE_SETUP_FETCHED": str(marker),
                        "CLIPFORGE_PREPARE_ONLY": "0",
                    },
                    check=True,
                )
                self.assertEqual(marker.exists(), fetched == "1")


class RemotePipelineTests(unittest.TestCase):
    def test_prepare_cache_uses_requested_profile_on_cpu_with_gpu_default(self):
        for requested in (None, "gpu", "cpu"):
            with (
                self.subTest(profile=requested),
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(
                    os.environ,
                    {
                        "CLIPFORGE_LOCAL_CACHE": temporary,
                        "CLIPFORGE_PERSISTENT_CACHE": "",
                        "CLIPFORGE_CPU_ONLY": "0",
                        "CLIPFORGE_CACHE_PROFILE": requested or "gpu",
                    },
                ),
            ):
                root = Path(temporary)
                work = root / "job"
                profile = requested or "gpu"
                key, cache_root, _ = cache.configuration()
                tag = cache.release_tag(key)
                parts = cache.release_parts()
                # An inherited profile must not override the payload or mode default.
                os.environ["CLIPFORGE_CACHE_PROFILE"] = (
                    "cpu" if profile == "gpu" else "gpu"
                )
                payload = {
                    "mode": "prepare-cache",
                    "source_ref": "b" * 40,
                    "workflow_run": "123",
                    "github_token": "test-only-github",
                    "accelerator": "NONE",
                }
                if requested is not None:
                    payload["cache_profile"] = requested
                bundles = Mock()
                bundles.restore.return_value = False
                with (
                    patch.object(remote_job, "ROOT", root),
                    patch.object(remote_job, "WORK", work),
                    patch.object(remote_job, "Path") as paths,
                    patch.object(remote_job, "ReleaseBundles", return_value=bundles),
                    patch.object(remote_job, "gpu_info") as gpu_info,
                    patch.object(remote_job, "host_info", return_value={}),
                    patch.object(remote_job, "read_setup_timings", return_value={}),
                    patch.object(remote_job.os, "umask"),
                    patch.object(remote_job, "command") as run,
                ):
                    paths.return_value.read_text.return_value = json.dumps(payload)
                    remote_job.main()
                gpu_info.assert_not_called()
                bundles.restore.assert_called_once_with(
                    tag, cache_root, "assets", key, parts=parts
                )
                self.assertIn(f"cache-{profile}-", tag)
                self.assertEqual("models/hub" in parts, profile == "gpu")
                bundles.publish.assert_called_once()
                published = bundles.publish.call_args
                self.assertEqual(published.args[:2], (tag, cache_root))
                self.assertEqual(published.args[3:], ("assets", key))
                self.assertEqual(published.kwargs["parts"], parts)
                run.assert_called_once()
                setup_env = run.call_args.args[1]
                self.assertEqual(setup_env["CLIPFORGE_CPU_ONLY"], "1")
                self.assertEqual(setup_env["CLIPFORGE_CACHE_PROFILE"], profile)
                self.assertEqual(setup_env["CLIPFORGE_SKIP_MODEL_PREPARE"], "0")
                self.assertEqual(setup_env["CLIPFORGE_PREPARE_ONLY"], "1")

    def test_parallel_audio_cache_is_skipped_for_cpu_and_saved_by_first_gpu_clip(self):
        for accelerator, cached in (
            ("NONE", False),
            ("NONE", True),
            ("T4", False),
            ("T4", True),
        ):
            for fetched in (False, True):
                for clip_index in (None, "1", "2"):
                    with (
                        self.subTest(
                            accelerator=accelerator,
                            cached=cached,
                            fetched=fetched,
                            clip_index=clip_index,
                        ),
                        tempfile.TemporaryDirectory() as temporary,
                    ):
                        root = Path(temporary)
                        work = root / "job"
                        payload = {
                            "mode": "audio-stage",
                            "request_id": "a" * 32,
                            "source_ref": "b" * 40,
                            "workflow_run": "123",
                            "github_token": "test-only-github",
                            "accelerator": accelerator,
                            "stage": {
                                "previous_run": "122",
                                "previous_request": "c" * 32,
                                "review": '{"clips": []}',
                                "clip_index": clip_index,
                            },
                        }

                        def command(arguments, environment, name, **kwargs):
                            if name == "setup" and fetched:
                                (work / "setup-fetched").touch()
                            if name == "render":
                                (work / "out/manifest.json").write_text(
                                    '{"status": "clip_rendered"}'
                                )

                        bundles = Mock()
                        bundles.restore.return_value = cached
                        with (
                            patch.object(remote_job, "ROOT", root),
                            patch.object(remote_job, "WORK", work),
                            patch.dict(os.environ, {}),
                            patch.object(remote_job, "Path") as paths,
                            patch.object(
                                remote_job,
                                "configuration",
                                return_value=("cache-key", root, root),
                            ),
                            patch.object(
                                remote_job, "ReleaseBundles", return_value=bundles
                            ),
                            patch.object(remote_job, "host_info", return_value={}),
                            patch.object(remote_job, "gpu_info", return_value={}),
                            patch.object(
                                remote_job, "read_setup_timings", return_value={}
                            ),
                            patch.object(
                                remote_job, "publish_result", return_value=True
                            ) as publish_result,
                            patch.object(remote_job.os, "umask"),
                            patch.object(remote_job, "mark_restored"),
                            patch.object(
                                remote_job, "command", side_effect=command
                            ) as run,
                        ):
                            paths.return_value.read_text.return_value = json.dumps(
                                payload
                            )
                            remote_job.main()
                        cpu_only = accelerator == "NONE"
                        profile = "cpu" if cpu_only else "gpu"
                        parts = (
                            cache.PARTS if cpu_only else (*cache.PARTS, "models/hub")
                        )
                        should_save = (
                            not cpu_only
                            and clip_index in (None, "1")
                            and (not cached or fetched)
                        )
                        publish_result.assert_called_once()
                        self.assertEqual(bundles.publish.call_count, int(should_save))
                        self.assertFalse(
                            any(
                                call.args[2] == "save-cache"
                                for call in run.call_args_list
                            )
                        )
                        report = json.loads((work / "out/gpu-receipt.json").read_text())
                        if cpu_only:
                            bundles.restore.assert_not_called()
                            self.assertEqual(
                                report["public_cache_restored"], "skipped_cpu_profile"
                            )
                            self.assertEqual(
                                report["public_cache_saved"], "skipped_cpu_profile"
                            )
                            self.assertNotIn("cache_restore", report["timings"])
                            self.assertNotIn("cache_publish", report["timings"])
                        else:
                            bundles.restore.assert_called_once_with(
                                "clipforge-cache-gpu-cache-key",
                                root,
                                "assets",
                                "cache-key",
                                parts=parts,
                            )
                            self.assertEqual(report["public_cache_restored"], cached)
                        setup_env = next(
                            call.args[1]
                            for call in run.call_args_list
                            if call.args[2] == "setup"
                        )
                        self.assertEqual(
                            setup_env["CLIPFORGE_CPU_ONLY"], "1" if cpu_only else "0"
                        )
                        self.assertEqual(setup_env["CLIPFORGE_CACHE_PROFILE"], profile)
                        self.assertEqual(setup_env["CLIPFORGE_OFFLINE"], "0")
                        self.assertEqual(setup_env["CLIPFORGE_PERSISTENT_CACHE"], "")
                        if should_save:
                            published = bundles.publish.call_args
                            self.assertEqual(
                                published.args[:2],
                                ("clipforge-cache-gpu-cache-key", root),
                            )
                            self.assertEqual(published.kwargs["parts"], parts)

    def test_pipeline_uses_staged_input_and_preserves_result_publication(self):
        for audio_analysis in (True, False):
            with (
                self.subTest(audio_analysis=audio_analysis),
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
                    "gemini_api_key": "" if audio_analysis else "test-only-gemini",
                }
                status = "topics_allocated" if audio_analysis else "complete"

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
                    patch.dict(os.environ, {}),
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
                    patch.object(remote_job, "mark_restored"),
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
                self.assertEqual(set(calls), {"setup", "prepare", "pipeline"})
                self.assertIn("tools/run_request.py", invocation)
                self.assertIn("run", invocation)
                self.assertIn("prepare", calls["prepare"].args[0])
                self.assertNotIn("GEMINI_API_KEY", calls["setup"].args[1])
                self.assertNotIn("GEMINI_API_KEY", calls["prepare"].args[1])
                self.assertNotIn("GH_TOKEN", environment)
                self.assertNotIn("GITHUB_TOKEN", environment)
                if audio_analysis:
                    self.assertNotIn("GEMINI_API_KEY", environment)
                else:
                    self.assertEqual(environment["GEMINI_API_KEY"], "test-only-gemini")
                self.assertEqual(environment["CLIPFORGE_REQUIRE_CUDA"], "1")
                self.assertEqual(environment["CLIPFORGE_CACHE_PROFILE"], "gpu")
                bundles.publish.assert_called_once()
                published = bundles.publish.call_args.args
                self.assertEqual(published[:2], (payload["input_tag"], output))
                self.assertEqual(published[3:], ("result", payload["request_id"]))
                report = json.loads((output / "gpu-receipt.json").read_text())
                self.assertEqual(report["status"], status)
                self.assertIn("pipeline", report["timings"])
                self.assertEqual(report["public_cache_saved"], "skipped_restored")


class RemoteCacheRecoveryTests(unittest.TestCase):
    def run_job(
        self,
        *,
        restore_fails=False,
        failure=None,
        cached=False,
        accelerator="T4",
        offline=False,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        work = root / "job"
        cache_root = root / "cache"
        cache_root.mkdir()
        keep = cache_root / "keep.txt"
        keep.write_text("synthetic unrelated data")
        payload_path = root / "payload.json"
        report_path = root / "report.json"
        payload = {
            "mode": "audio-stage",
            "request_id": "a" * 32,
            "source_ref": "b" * 40,
            "workflow_run": "123",
            "github_token": "test-only-github",
            "accelerator": accelerator,
            "offline": offline,
            "stage": {
                "previous_run": "122",
                "previous_request": "c" * 32,
                "review": '{"clips": []}',
                "clip_index": "1",
            },
        }
        payload_path.write_text(json.dumps(payload))
        parts = (*cache.PARTS, "models/hub")
        bundles = Mock()

        def restore(*args, **kwargs):
            if not restore_fails:
                return cached
            for name in parts:
                path = cache_root / name
                path.mkdir(parents=True)
                (path / "partial").write_bytes(b"synthetic partial cache")
            shutil.rmtree(cache_root / "pip")
            (cache_root / "pip").symlink_to(keep)
            (cache_root / ".restored").write_text("synthetic stale marker")
            (cache_root / ".saved-inventory").write_text("synthetic stale inventory")
            raise BundleError("synthetic private detail must not be reported")

        bundles.restore.side_effect = restore
        commands = []

        def command(arguments, environment, name):
            commands.append(name)
            self.assertEqual(environment["CLIPFORGE_OFFLINE"], "1" if offline else "0")
            if name == "setup" and restore_fails:
                self.assertTrue(all(not (cache_root / name).exists() for name in parts))
                self.assertFalse((cache_root / "pip").is_symlink())
                self.assertFalse((cache_root / ".restored").exists())
                self.assertFalse((cache_root / ".saved-inventory").exists())
                self.assertEqual(keep.read_text(), "synthetic unrelated data")
            if name == failure:
                raise RuntimeError("synthetic stage failure")
            if name == "render":
                (work / "out/manifest.json").write_text('{"status": "clip_rendered"}')

        def path(value):
            return {
                "/content/.clipforge-job.json": payload_path,
                "/content/clipforge-report.json": report_path,
            }.get(str(value), Path(value))

        with (
            patch.object(remote_job, "ROOT", root),
            patch.object(remote_job, "WORK", work),
            patch.object(remote_job, "Path", side_effect=path),
            patch.dict(os.environ, {}),
            patch.object(
                remote_job, "configuration", return_value=("key", cache_root, None)
            ),
            patch.object(remote_job, "ReleaseBundles", return_value=bundles),
            patch.object(remote_job, "gpu_info", return_value={}),
            patch.object(remote_job, "host_info", return_value={}),
            patch.object(remote_job, "mark_restored"),
            patch.object(
                remote_job, "publish_result", return_value=True
            ) as publish_result,
            patch.object(remote_job.os, "umask"),
            patch.object(remote_job, "command", side_effect=command),
        ):
            if failure is None:
                remote_job.main()
            else:
                with self.assertRaisesRegex(RuntimeError, "Colab job failed"):
                    remote_job.main()
        publish_result.assert_called_once()
        return json.loads(report_path.read_text()), bundles, commands

    def test_partial_restore_is_removed_and_setup_continues_as_a_cache_miss(self):
        report, bundles, commands = self.run_job(restore_fails=True)
        self.assertEqual(report["cache_restore_error"], "BundleError")
        self.assertFalse(report["public_cache_restored"])
        self.assertEqual(report["status"], "clip_rendered")
        self.assertEqual(commands, ["setup", "render"])
        bundles.publish.assert_called_once()
        self.assertNotIn("synthetic private detail", json.dumps(report))

    def test_setup_failure_never_publishes_cache_but_later_failure_can(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                report, bundles, commands = self.run_job(failure="setup", cached=cached)
                self.assertEqual(report["public_cache_saved"], "skipped_setup_failed")
                self.assertEqual(report["status"], "failed")
                self.assertEqual(commands, ["setup"])
                bundles.publish.assert_not_called()
        report, bundles, commands = self.run_job(failure="render")
        self.assertTrue(report["public_cache_saved"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(commands, ["setup", "render"])
        bundles.publish.assert_called_once()

    def test_cpu_failures_keep_cache_skip_report_including_offline_setup(self):
        for failure, offline in (("setup", False), ("setup", True), ("render", False)):
            with self.subTest(failure=failure, offline=offline):
                report, bundles, commands = self.run_job(
                    failure=failure, accelerator="NONE", offline=offline
                )
                bundles.restore.assert_not_called()
                bundles.publish.assert_not_called()
                self.assertEqual(report["public_cache_restored"], "skipped_cpu_profile")
                self.assertEqual(report["public_cache_saved"], "skipped_cpu_profile")
                self.assertEqual(report["status"], "failed")
                self.assertEqual(
                    commands, ["setup"] if failure == "setup" else ["setup", "render"]
                )


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

    def test_download_window_refills_only_after_a_chunk_is_deleted(self):
        import colab_bundle

        for kind, tag, identity in (
            ("assets", "clipforge-cache-" + "a" * 24, "key"),
            ("result", "clipforge-input-" + "a" * 32, "request"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bundles, source = self.publish_many_chunks(root, kind, tag, identity)
                paths = []
                outstanding = []

                def submit(function, asset, path, chunk):
                    # Downloads finish immediately while extraction makes no progress.
                    # A new submission must wait until an earlier file has been deleted.
                    self.assertLess(
                        sum(p.exists() for p in paths),
                        colab_bundle.DOWNLOAD_WORKERS + 1,
                    )
                    future = Future()
                    future.set_result(function(asset, path, chunk))
                    paths.append(path)
                    outstanding.append(sum(p.exists() for p in paths))
                    return future

                with patch.object(colab_bundle, "ThreadPoolExecutor") as executor:
                    executor.return_value.__enter__.return_value.submit.side_effect = (
                        submit
                    )
                    bundles.restore(
                        tag, root / "restored", kind, identity, required=True
                    )
                self.assertGreater(len(paths), colab_bundle.DOWNLOAD_WORKERS + 1)
                self.assertEqual(max(outstanding), colab_bundle.DOWNLOAD_WORKERS + 1)
                self.assertFalse(any(p.exists() for p in paths))
                self.assertEqual(
                    (root / "restored/blob.bin").read_bytes(),
                    (source / "blob.bin").read_bytes(),
                )


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

        bundles = Mock()
        bundles.restore.return_value = False
        with (
            patch.object(remote_job, "ROOT", root),
            patch.object(remote_job, "WORK", work),
            patch.dict(os.environ, {}),
            patch.object(remote_job, "Path") as paths,
            patch.object(
                remote_job,
                "configuration",
                return_value=("cache-key", root, root),
            ),
            patch.object(remote_job, "ReleaseBundles", return_value=bundles),
            patch.object(remote_job, "host_info", return_value={}),
            patch.object(remote_job, "gpu_info", return_value={}),
            patch.object(remote_job, "read_setup_timings", return_value={}),
            patch.object(remote_job, "publish_result", return_value=True),
            patch.object(remote_job.os, "umask"),
            patch.object(remote_job, "mark_restored"),
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
