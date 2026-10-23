"""Owned, pinned YouTube transport on disposable GitHub-hosted runners."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from urllib.request import ProxyHandler, build_opener, urlopen

from .media_sources import source_kind
from .storage import PipelineError, write_json

YT_DLP_VERSION = "2026.10.21.233146dev"
PROVIDER_VERSION = "2.0.2"
WARP_VERSION = "2026.8.2115.0"
NODE_VERSION = "22.23.3"
PROVIDER_IMAGE = "brainicism/bgutil-ytdlp-pot-provider@sha256:d7d34108b76e52c7d3b575fe9e797e4bf41c1b2a08f80c5292eff6f851ce8472"
ROOT = Path(__file__).resolve().parents[2]


def now():
    return datetime.now(timezone.utc).isoformat()


def runner_identity():
    run, attempt = (
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    )
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or not re.fullmatch(r"[1-9][0-9]*", run)
        or not re.fullmatch(r"[1-9][0-9]*", attempt)
    ):
        raise PipelineError(
            "YouTube WARP transport requires an owned disposable GitHub-hosted runner"
        )
    return run + "-" + attempt


def route():
    # Use the normal process network/proxy configuration, never a separate route.
    with urlopen("https://www.cloudflare.com/cdn-cgi/trace", timeout=8) as response:
        fields = dict(
            line.split("=", 1)
            for line in response.read(16384).decode().splitlines()
            if "=" in line
        )
    if fields.get("warp") not in {"on", "off", "plus"} or not fields.get("ip"):
        raise PipelineError("network route could not be verified")
    return {
        "warp": fields.get("warp") in {"on", "plus"},
        "egress_sha256": hashlib.sha256(fields.get("ip", "").encode()).hexdigest()
        if fields.get("ip")
        else None,
    }


def health():
    # Loopback provider is deliberately unauthenticated and never goes through a proxy.
    with build_opener(ProxyHandler({})).open(
        "http://127.0.0.1:4416/ping", timeout=3
    ) as response:
        return json.load(response)


class Transport:
    def __init__(self, directory, url=None):
        self.owner = runner_identity()
        self.root = Path(directory)
        if any(
            char in str(self.root) for char in "\r\n"
        ) or not self.root.resolve().is_relative_to(
            Path(os.environ.get("RUNNER_TEMP", "/nonexistent-runner-temp")).resolve()
        ):
            raise PipelineError("transport state must stay inside RUNNER_TEMP")
        if self.root.is_symlink():
            raise PipelineError("transport state must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / "transport.json"
        if self.path.is_symlink():
            raise PipelineError("transport state must not be a symlink")
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None
        self.container = "clipforge-pot-" + self.owner
        self.rollback = "clipforge-youtube-" + self.owner + "-rollback"
        if self.state is not None and (
            self.state.get("owner") != self.owner
            or self.state.get("container") != self.container
        ):
            raise PipelineError("transport state belongs to another run")
        if url is not None:
            if source_kind(url) != "youtube":
                raise PipelineError("WARP transport requires a YouTube URL")
            self.source_hash = hashlib.sha256(url.encode()).hexdigest()
            if (
                self.state is not None
                and self.state.get("source_sha256") != self.source_hash
            ):
                raise PipelineError("transport state belongs to another source")
        else:
            self.source_hash = None

    def save(self):
        self.state["updated_at"] = now()
        write_json(self.path, self.state)
        self.path.chmod(0o600)

    @contextmanager
    def phase(self, name):
        self.state["phase"] = name
        self.save()
        started = time.monotonic()
        try:
            yield
        finally:
            self.state["timings"][name] = round(time.monotonic() - started, 3)
            self.save()

    def command(self, arguments, timeout=180, environment=None, allow_failure=False):
        environment = dict(os.environ if environment is None else environment)
        for name in (
            "YOUTUBE_COOKIES_B64",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GEMINI_API_KEY",
            "COLAB_ADC_JSON",
        ):
            environment.pop(name, None)
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout, env=environment
        )
        if result.returncode and not allow_failure:
            # Commands can emit network headers: never copy raw bodies into public logs.
            raise PipelineError(
                "YouTube transport command failed in phase " + self.state["phase"]
            )
        return result

    def provider_ready(self):
        for attempt in range(20):
            try:
                running = self.command(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Running}}",
                        self.container,
                    ],
                    timeout=10,
                ).stdout.strip()
                if running != "true":
                    raise PipelineError("owned PO Token container is not running")
                result = health()
                if result.get("version") != PROVIDER_VERSION:
                    raise PipelineError("unexpected PO Token server version")
                self.state["provider_health"] = result
                self.save()
                return
            except (OSError, ValueError):
                if attempt == 19:
                    raise PipelineError(
                        "PO Token server did not become ready"
                    ) from None
                time.sleep(0.5)

    def prepare(self):
        if self.state is not None:
            raise PipelineError(
                "transport state already exists; automatic restart is disabled"
            )
        if self.source_hash is None:
            raise PipelineError("transport setup requires a source")
        self.state = {
            "schema": 1,
            "owner": self.owner,
            "container": self.container,
            "source_sha256": self.source_hash,
            "status": "starting",
            "started_at": now(),
            "timings": {},
            "owns_warp": False,
            "instance": uuid.uuid4().hex,
            "provider_image": PROVIDER_IMAGE,
        }
        self.save()
        with self.phase("initial_route"):
            initial = route()
            if initial["warp"]:
                raise PipelineError(
                    "a WARP route already exists; refusing to replace it"
                )
            self.state["initial_route"] = initial
        with self.phase("dependencies"):
            lock = ROOT / "tools/youtube-transport-requirements.txt"
            self.state["requirements_sha256"] = hashlib.sha256(
                lock.read_bytes()
            ).hexdigest()
            self.command(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--require-hashes",
                    "-r",
                    str(lock),
                ],
                timeout=240,
            )
            versions = self.command(
                [
                    sys.executable,
                    "-c",
                    "from importlib.metadata import version; import json; print(json.dumps({name:version(name) for name in ['yt-dlp','bgutil-ytdlp-pot-provider','curl-cffi','yt-dlp-ejs']}))",
                ]
            )
            self.state["versions"] = json.loads(versions.stdout)
            node = self.command(["node", "--version"]).stdout.strip().removeprefix("v")
            if node != NODE_VERSION:
                raise PipelineError(
                    "unexpected Node.js version for the verified YouTube profile"
                )
            self.state["versions"]["node"] = node
            if (
                self.state["versions"]["yt-dlp"] != YT_DLP_VERSION
                or self.state["versions"]["bgutil-ytdlp-pot-provider"]
                != PROVIDER_VERSION
            ):
                raise PipelineError("installed YouTube transport version mismatch")
        with self.phase("provider_start"):
            self.command(["docker", "pull", PROVIDER_IMAGE], timeout=240)
            self.state["provider_create_attempted"] = True
            self.save()
            self.command(
                [
                    "docker",
                    "run",
                    "-d",
                    "--init",
                    "--network",
                    "host",
                    "--name",
                    self.container,
                    "--label",
                    "com.clipforge.owner=" + self.owner,
                    "--label",
                    "com.clipforge.transport=" + self.state["instance"],
                    PROVIDER_IMAGE,
                    "--host",
                    "127.0.0.1",
                ]
            )
            self.provider_ready()
        self.state.update(status="prepared", phase="prepared")
        self.save()

    def connect(self):
        if self.state is None or self.state.get("status") != "prepared":
            raise PipelineError("transport must be prepared before connecting WARP")
        with self.phase("warp_connect"):
            self.state["owns_warp"] = True
            self.save()
            environment = {
                **os.environ,
                "CLIPFORGE_YOUTUBE_REPORT_DIR": str(self.root.resolve()),
                "CLIPFORGE_YOUTUBE_ROLLBACK_UNIT": self.rollback,
            }
            self.command(
                ["bash", str(ROOT / "tools/connect_youtube_warp.sh")],
                timeout=240,
                environment=environment,
            )
            installed = self.command(
                ["dpkg-query", "-W", "-f=${Version}", "cloudflare-warp"]
            ).stdout.strip()
            if installed != WARP_VERSION:
                raise PipelineError("unexpected WARP version")
            self.state["versions"]["cloudflare-warp"] = installed
            verified = route()
            if not verified["warp"] or not verified["egress_sha256"]:
                raise PipelineError("WARP route is not verified")
            self.state["ready_route"] = verified
        with self.phase("provider_reset"):
            # Tokens may be IP-bound. Discard every pre-WARP minter/session cache.
            self.command(["docker", "restart", self.container])
            self.provider_ready()
        self.state.update(status="ready", phase="ready", ready_at=now())
        self.save()
        if os.environ.get("GITHUB_ENV"):
            with open(os.environ["GITHUB_ENV"], "a") as handle:
                handle.write(
                    "CLIPFORGE_YOUTUBE_TRANSPORT_STATE="
                    + str(self.root.resolve())
                    + "\n"
                )

    def verify(self):
        if self.state is None or self.state.get("status") != "ready":
            raise PipelineError("YouTube WARP transport is not ready")
        current = route()
        if (
            not current["warp"]
            or current["egress_sha256"] != self.state["ready_route"]["egress_sha256"]
        ):
            raise PipelineError(
                "YouTube transport route changed; refusing cached tokens"
            )
        if health().get("version") != PROVIDER_VERSION:
            raise PipelineError("PO Token provider is not ready")
        return self.state

    def cleanup(self):
        if self.state is None or self.state.get("status") == "cleaned":
            return
        errors = []
        with self.phase("cleanup"):
            if self.state.get("owns_warp"):
                try:
                    self.command(
                        ["timeout", "15", "warp-cli", "--accept-tos", "disconnect"],
                        timeout=20,
                    )
                    for attempt in range(3):
                        try:
                            if route()["warp"]:
                                raise PipelineError("WARP is still connected")
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                            time.sleep(1)
                    self.state["warp_disconnected"] = True
                    self.command(
                        ["sudo", "systemctl", "stop", self.rollback + ".timer"],
                        timeout=10,
                        allow_failure=True,
                    )
                except Exception:
                    errors.append("warp_disconnect")
            try:
                present = (
                    self.command(
                        [
                            "docker",
                            "ps",
                            "-a",
                            "--filter",
                            "name=^" + self.container + "$",
                            "--format",
                            "{{.Names}}",
                        ],
                        timeout=10,
                    ).stdout.strip()
                    if self.state.get("provider_create_attempted")
                    else ""
                )
                if present:
                    inspected = self.command(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            '{{ index .Config.Labels "com.clipforge.owner" }} {{ index .Config.Labels "com.clipforge.transport" }}',
                            self.container,
                        ],
                        timeout=10,
                    )
                    if (
                        inspected.stdout.strip()
                        != self.owner + " " + self.state["instance"]
                    ):
                        raise PipelineError("refusing to remove a foreign container")
                    self.command(["docker", "rm", "-f", self.container], timeout=15)
                self.state["provider_removed"] = True
            except Exception:
                errors.append("provider_remove")
            self.state.update(
                status="cleanup_failed" if errors else "cleaned", cleanup_errors=errors
            )
        if errors:
            raise PipelineError(
                "YouTube transport cleanup failed: " + ", ".join(errors)
            )


def verify_ready(url):
    directory = os.environ.get("CLIPFORGE_YOUTUBE_TRANSPORT_STATE")
    if not directory:
        raise PipelineError(
            "YouTube WARP acquisition requires an initialized transport state"
        )
    return Transport(directory, url).verify()
