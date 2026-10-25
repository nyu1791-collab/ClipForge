#!/usr/bin/env python3
"""Actions control plane using Google's Colab CLI SDK; secrets stay off the logs."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from colab_bundle import ReleaseBundles, digest

EXPECTED_ACCOUNT = "n.yu17919191@gmail.com"
SOURCE_REPO = "https://github.com/nyu1791-collab/ClipForge.git"
REMOTE_PAYLOAD_PATH = "/content/.clipforge-job.json"
# Every rendered clip must list these; audio-first rows may list more (ledger.json, quality.json).
REQUIRED_CLIP_FILES = frozenset({"video.mp4", "captions.ass", "captions.srt"})


class ControlError(RuntimeError):
    pass


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def identifiers():
    request_id = os.environ.get("REQUEST_ID", "")
    source = os.environ.get("CLIPFORGE_SOURCE_REF", "")
    tag = os.environ.get("INPUT_TAG", "")
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ControlError("Invalid request ID")
    if not re.fullmatch(r"[a-f0-9]{40}", source):
        raise ControlError("Use an exact ClipForge source SHA")
    if tag != "clipforge-input-" + request_id:
        raise ControlError("Input release and request ID differ")
    return {"request_id": request_id, "source_ref": source, "input_tag": tag}


def credentials_from_info(info):
    if info.get("type") != "authorized_user":
        raise ControlError("Colab Pro requires the intended end-user OAuth credentials")
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(info)


def authenticated_client(info):
    from google.auth.transport.requests import AuthorizedSession
    from colab_cli.client import Client, Prod

    session = AuthorizedSession(credentials_from_info(info))
    response = session.get("https://www.googleapis.com/oauth2/v2/userinfo", timeout=30)
    if response.status_code != 200:
        raise ControlError(
            "Google user identity could not be verified; no GPU was requested"
        )
    identity = response.json()
    if identity.get("email", "").lower() != EXPECTED_ACCOUNT or not identity.get(
        "verified_email"
    ):
        raise ControlError("Google account mismatch; no GPU was requested")
    return Client(Prod(), session)


def runtime_from_state(record):
    from colab_cli.runtime import ColabRuntime

    return ColabRuntime(
        record["url"],
        record["token"],
        kernel_id=record.get("kernel_id"),
        session_id=record.get("session_id"),
    )


def checked_execute(runtime, code, timeout):
    outputs = runtime.execute_code(code, allow_stdin=False, timeout=timeout)
    if any(item.get("output_type") == "error" for item in outputs):
        raise ControlError("Colab code failed; no generation was retried")
    return outputs


def compute_units(client):
    """Account balance, hourly rate and active runtimes; None if Colab does not report them."""
    try:
        info = client.get_consumption_user_info()
        return {
            "balance": round(float(info.paid_compute_units_balance), 2),
            "hourly_rate": round(float(info.consumption_rate_hourly), 2),
            "assignments": int(info.assignments_count),
        }
    except Exception:
        return None


def print_units(label, units):
    if units is None:
        print(f"Compute units ({label}): not reported", flush=True)
    else:
        print(
            f"Compute units ({label}): balance {units['balance']:.2f}, "
            f"rate {units['hourly_rate']:.2f}/h, active runtimes {units['assignments']}",
            flush=True,
        )


def host_text(host):
    """Cores and memory of the runtime, so a log shows which shape actually did the work."""
    if not isinstance(host, dict) or not isinstance(host.get("cpu_count"), int):
        return ""
    return f" host: {host['cpu_count']} cores, {host.get('memory_gib')} GiB"


def colab_shape():
    """The runtime shape named by CLIPFORGE_COLAB_SHAPE: standard (default) or high-ram."""
    from colab_cli.client import Shape

    shapes = {"standard": Shape.STANDARD, "high-ram": Shape.HIGH_RAM}
    name = os.environ.get("CLIPFORGE_COLAB_SHAPE") or "standard"
    if name not in shapes:
        raise ControlError("Unknown Colab shape: use standard or high-ram")
    return shapes[name]


# Production rendering requires NVENC; allocate a GPU and never silently fall back to CPU.
# Try the next GPU only on TooManyAssignmentsError (412).
# Measured 2026-10-22: at most two T4 and two
# L4 at once, counted per kind; three CPU high-ram and two CPU standard were granted together.
# Rates then: CPU high-ram 0.26, T4 1.07, T4 high-ram 1.27, L4 1.54 compute units an hour.
# Measured 2026-10-22: a second pipeline's T4 high-ram was refused; capacity varies.
DEFAULT_POOLS = {
    "asr": "T4:high-ram,T4:standard,L4",
    "audio-stage": "L4,T4:high-ram,T4:standard",
}


def colab_pool(mode):
    """Ordered (accelerator, shape) candidates; CLIPFORGE_COLAB_POOL overrides the default."""
    from colab_cli.client import Accelerator, Shape

    raw = os.environ.get("CLIPFORGE_COLAB_POOL") or DEFAULT_POOLS[mode]
    kinds = {"CPU": Accelerator.NONE, "T4": Accelerator.T4, "L4": Accelerator.L4}
    shapes = {"standard": Shape.STANDARD, "high-ram": Shape.HIGH_RAM}
    pool = []
    for item in raw.split(","):
        kind, _, shape = item.strip().partition(":")
        if kind not in kinds or (shape or "standard") not in shapes or len(pool) >= 6:
            raise ControlError(
                "Invalid Colab pool; use entries like CPU:high-ram or L4"
            )
        # L4 exists only in one high-memory shape; Colab ignores the shape parameter for it.
        pool.append((kinds[kind], shapes[shape or "standard"]))
    return pool


def stage_request():
    """The reviewed render this job performs, from the workflow's validated inputs."""
    stage = {
        "audio_stage": os.environ.get("AUDIO_STAGE", ""),
        "previous_run": os.environ.get("PREVIOUS_RUN", ""),
        "previous_request": os.environ.get("PREVIOUS_REQUEST", ""),
        "clip_index": os.environ.get("CLIP_INDEX") or None,
        "config_file": os.environ.get("CONFIG_FILE") or None,
    }
    if (
        stage["audio_stage"] != "render"
        or not re.fullmatch(r"[0-9]{1,20}", stage["previous_run"])
        or not re.fullmatch(r"[a-f0-9]{32}", stage["previous_request"])
        or (
            stage["clip_index"] is not None
            and not re.fullmatch(r"[1-9][0-9]?", stage["clip_index"])
        )
        or (
            stage["config_file"] is not None
            and not re.fullmatch(
                r"config\.[a-z0-9-]+\.example\.json", stage["config_file"]
            )
        )
    ):
        raise ControlError("Invalid audio stage request")
    review = Path(os.environ.get("REVIEW_FILE", "")).read_text(encoding="utf-8")
    # The workflow input is bounded at 60,000 bytes; this is the same bound for the file.
    if len(review.encode()) > 60_000:
        raise ControlError("Review is too large")
    json.loads(review)
    stage["review"] = review
    return stage


def lookup_assignment(client, notebook_hash, variant, accelerator, shape):
    """Reconcile only this notebook, never infer ownership from account-wide changes."""
    from colab_cli.client import Assignment, GetAssignmentResponse

    assignment = client._get_assignment(notebook_hash, variant, accelerator, shape)
    if isinstance(assignment, Assignment):
        return assignment
    if isinstance(assignment, GetAssignmentResponse):
        return None
    raise ControlError("Unexpected Colab assignment response")


class Controller:
    def __init__(self, root):
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.credentials_path = self.root / "adc.json"

    def state(self):
        return json.loads(self.state_path.read_text())

    def client(self):
        return authenticated_client(json.loads(self.credentials_path.read_text()))

    def reconcile_assignment(self, client, record):
        from colab_cli.client import Accelerator, Shape, Variant

        record["status"] = "uncertain"
        write_private(self.state_path, record)
        accelerator = Accelerator(record["accelerator"])
        for attempt in range(3):
            assignment = lookup_assignment(
                client,
                uuid.UUID(record["notebook_hash"]),
                Variant.DEFAULT if accelerator is Accelerator.NONE else Variant.GPU,
                accelerator,
                Shape[record["shape"]],
            )
            if assignment is not None:
                break
            if attempt < 2:
                time.sleep(10)
        if assignment is not None:
            record.update(
                status="allocated",
                endpoint=assignment.endpoint,
                url=assignment.runtime_proxy_info.url,
                token=assignment.runtime_proxy_info.token,
            )
        else:
            record["status"] = "stopped"
            for key in ("endpoint", "url", "token"):
                record.pop(key, None)
        write_private(self.state_path, record)
        if assignment is None:
            self.credentials_path.unlink(missing_ok=True)
        return assignment

    def start(self, mode="asr"):
        if mode not in DEFAULT_POOLS:
            raise ControlError("Unknown Colab job mode")
        request = identifiers()
        pool = colab_pool(mode)
        try:
            allocation_wait = int(
                os.environ.get("CLIPFORGE_ALLOCATION_WAIT_SECONDS", "0")
            )
        except ValueError:
            raise ControlError(
                "Allocation wait must be a non-negative integer"
            ) from None
        if allocation_wait < 0:
            raise ControlError("Allocation wait must be a non-negative integer")
        if mode == "audio-stage":
            request["stage"] = stage_request()
        if self.state_path.is_file() and self.state().get("status") != "stopped":
            raise ControlError("This control directory already tracks a runtime")
        raw = os.environ.get("COLAB_ADC_JSON", "")
        if not raw:
            raise ControlError(
                "COLAB_ADC_JSON is not configured: one-time Google OAuth consent is required"
            )
        if len(raw) > 256 * 1024:
            raise ControlError("OAuth credential configuration is too large")
        info = json.loads(raw)
        client = authenticated_client(info)
        # A read-only permission check comes before allocation; never use another account.
        client.list_assignments()
        units = compute_units(client)
        print_units("before allocation", units)
        write_private(self.credentials_path, info)
        record = {
            **request,
            "mode": mode,
            "status": "creating",
            "account": EXPECTED_ACCOUNT,
            "units_before": units,
            "workflow_run": os.environ.get("GITHUB_RUN_ID"),
            "result_transport": "final-release"
            if mode == "audio-stage" and os.environ.get("RESULT_STORE") == "release"
            else "input-bundle",
        }
        write_private(self.state_path, record)
        from colab_cli.client import Accelerator, TooManyAssignmentsError, Variant

        allocation_started = time.monotonic()
        wait_attempt = 0
        while True:
            refused = []
            for accelerator, shape in pool:
                variant = (
                    Variant.DEFAULT if accelerator is Accelerator.NONE else Variant.GPU
                )
                notebook_hash = uuid.uuid4()
                record.update(
                    accelerator=accelerator.value,
                    shape=shape.name,
                    notebook_hash=str(notebook_hash),
                )
                write_private(self.state_path, record)
                try:
                    assignment = client.assign(
                        notebook_hash,
                        variant=variant,
                        accelerator=accelerator,
                        shape=shape,
                    )
                    break
                except TooManyAssignmentsError:
                    refused.append(f"{accelerator.value}/{shape.name}")
                    print(f"Colab refused {refused[-1]}", flush=True)
                    continue
                except Exception as error:
                    record["allocation_error"] = type(error).__name__
                    try:
                        self.reconcile_assignment(client, record)
                    except Exception:
                        # Keep the UUID and credentials so stop() can retry reconciliation.
                        pass
                    raise
            else:
                elapsed = time.monotonic() - allocation_started
                remaining = allocation_wait - elapsed
                if remaining <= 0:
                    record.update(status="stopped", allocation_refused=refused)
                    write_private(self.state_path, record)
                    self.credentials_path.unlink(missing_ok=True)
                    raise ControlError(
                        "Colab refused every runtime in the pool; no other account was tried"
                    )
                wait_attempt += 1
                print(
                    f"割り当て待ち: {wait_attempt}回目、経過{elapsed:.0f}秒、"
                    f"拒否された種類: {', '.join(refused)}",
                    flush=True,
                )
                # Explicit refusals allocate nothing, so waiting uses no Colab runtime.
                time.sleep(min(60, remaining))
                continue
            break
        # Persist ownership immediately, before kernel setup or any job execution.
        record.update(
            status="allocated",
            allocated_at=round(time.time(), 1),
            endpoint=assignment.endpoint,
            url=assignment.runtime_proxy_info.url,
            token=assignment.runtime_proxy_info.token,
        )
        write_private(self.state_path, record)
        print(
            "Colab runtime allocated for the verified account:",
            accelerator.value,
            shape.name,
            flush=True,
        )

    def run(self, output):
        record = self.state()
        if record.get("status") != "allocated":
            raise ControlError("No owned Colab runtime is available")
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise ControlError("Private GitHub input authentication is missing")
        payload = {
            **record,
            "github_token": token,
            "github_repository": os.environ.get(
                "GITHUB_REPOSITORY", "nyu1791-collab/ClipForge"
            ),
            "workflow_run": record.get("workflow_run")
            or os.environ.get("GITHUB_RUN_ID", "manual"),
        }
        if record["mode"] == "audio-stage":
            payload["youtube_cookies_b64"] = os.environ.get("YOUTUBE_COOKIES_B64", "")
        # Google OAuth/Colab proxy credentials are NOT uploaded to the GPU worker.
        for name in ("url", "token", "endpoint", "status"):
            payload.pop(name, None)
        private_payload = self.root / "job-payload.json"
        write_private(private_payload, payload)
        from colab_cli.contents import ContentsClient
        from colab_cli.state import SessionState

        remote_state = SessionState(
            name="clipforge",
            token=record["token"],
            url=record["url"],
            endpoint=record["endpoint"],
        )
        contents = ContentsClient(remote_state)
        runtime = runtime_from_state(record)
        execution_error = None
        try:
            contents.upload(str(private_payload), REMOTE_PAYLOAD_PATH)
            source = record["source_ref"]
            bootstrap = f"""
import json, pathlib, runpy, subprocess, sys

def _clipforge_bootstrap():
    payload_path = pathlib.Path({REMOTE_PAYLOAD_PATH!r})
    try:
        subprocess.run(['git','clone','--no-checkout','--filter=blob:none',{SOURCE_REPO!r},'/content/ClipForge'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['git','-C','/content/ClipForge','checkout','--detach',{source!r}],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.path.insert(0, '/content/ClipForge/tools')
        runpy.run_path('/content/ClipForge/tools/colab_remote_job.py', run_name='__main__')
    finally:
        payload_path.unlink(missing_ok=True)
_clipforge_bootstrap()
del _clipforge_bootstrap
"""
            print(
                "Running the dedicated Colab worker; no browser interaction is required.",
                flush=True,
            )
            try:
                checked_execute(runtime, bootstrap, 5100)
            except Exception as error:
                execution_error = error
                try:
                    checked_execute(
                        runtime,
                        f"from pathlib import Path; Path({REMOTE_PAYLOAD_PATH!r}).unlink(missing_ok=True)",
                        30,
                    )
                except Exception:
                    pass
        finally:
            private_payload.unlink(missing_ok=True)
            try:
                runtime.stop()
            except Exception:
                if execution_error is None:
                    raise
        release_error = None
        # The results are already published in the Release, so the runtime is freed before
        # downloading them. stop() is idempotent; the workflow's final stop step retries if
        # this release is not confirmed.
        try:
            self.stop()
        except Exception as error:
            release_error = error
        try:
            self.collect(record, output, execution_error is not None, token)
        except Exception:
            if execution_error is not None:
                raise execution_error from None
            raise
        finally:
            self.report_stages(output)
            if release_error is not None:
                print(
                    "Early Colab release failed: " + type(release_error).__name__,
                    file=sys.stderr,
                )
        if execution_error is not None:
            raise execution_error
        if release_error is not None:
            raise ControlError(
                "Colab runtime release was not confirmed after results were retrieved"
            )

    def report_stages(self, output):
        # Prints only public stage timings and the error class recorded by the worker.
        for name in ("gpu-receipt.json", "colab-report.json"):
            path = Path(output) / name
            if not path.is_file():
                continue
            receipt = json.loads(path.read_text())
            print(
                "Colab stage: "
                + str(receipt.get("stage"))
                + " error: "
                + str(receipt.get("error_type"))
                + " timings: "
                + str(receipt.get("timings", {}))
                + host_text(receipt.get("host")),
                flush=True,
            )
            # ASCII-only package and CUDA lines from a failed ASR stage; never speech text.
            for line in receipt.get("log_tail") or []:
                if isinstance(line, str) and line.isascii():
                    print("Colab log: " + line[:300], flush=True)
            worker_error = receipt.get("worker_error")
            if isinstance(worker_error, dict):
                line = "Worker error: " + str(worker_error.get("error_type"))
                if worker_error.get("message"):
                    line += ": " + str(worker_error["message"])
                print(line, flush=True)
            return

    def collect(self, record, output, execution_failed, token):
        if record.get("result_transport") == "final-release":
            # Only execution records return to Actions. The large MP4s stay in the
            # final Release for the collector/Drive to download once when needed.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from clipforge.release_transfer import read_transfer

            data = read_transfer(
                record["workflow_run"], "clipforge-result-" + record["request_id"]
            )
            receipt = data["request_receipt"]
            manifest = data.get("stage_manifest", {})
            output = Path(output)
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_private(output / "request-receipt.json", receipt)
            write_private(output / "manifest.json", manifest)
            if data.get("gpu_receipt") is not None:
                write_private(output / "gpu-receipt.json", data["gpu_receipt"])
            expected = "clip_rendered" if record["stage"]["clip_index"] else "complete"
            if (
                receipt.get("request_id") != record["request_id"]
                or receipt.get("source_ref") != record["source_ref"]
            ):
                raise ControlError(
                    "Returned Colab result identity differs from the request"
                )
            if (
                execution_failed
                or receipt.get("status") != expected
                or manifest.get("status") != expected
            ):
                raise ControlError(
                    "Colab job failed; private partial outputs remain in the final Release"
                )
            return
        ReleaseBundles(token).restore(
            record["input_tag"], output, record["request_id"], required=True
        )
        receipt = json.loads((Path(output) / "request-receipt.json").read_text())
        if (
            receipt.get("request_id") != record["request_id"]
            or receipt.get("source_ref") != record["source_ref"]
        ):
            raise ControlError(
                "Returned Colab result identity differs from the request"
            )
        manifest = Path(output) / "manifest.json"
        # An ASR part ends at asr_part_complete, one reviewed clip at clip_rendered and a
        # render of every clip at complete.
        if record["mode"] == "asr":
            expected = "asr_part_complete"
        else:
            expected = "clip_rendered" if record["stage"]["clip_index"] else "complete"
        complete = (
            manifest.is_file()
            and json.loads(manifest.read_text()).get("status") == expected
        )
        if execution_failed or receipt.get("status") != expected or not complete:
            raise ControlError(
                "Colab job failed; available private partial outputs were retrieved"
            )
        for name, checksum in receipt.get("sha256", {}).items():
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){0,3}", name)
                or ".." in name
            ):
                raise ControlError("Unexpected result file name")
            if digest(Path(output) / name) != checksum:
                raise ControlError("Colab result checksum mismatch")
        rendered = json.loads(manifest.read_text()).get("rendered", [])
        if record["mode"] == "audio-stage" and record["stage"]["clip_index"]:
            directory = "clips/clip-%03d" % int(record["stage"]["clip_index"])
            if (
                not isinstance(rendered, list)
                or len(rendered) != 1
                or not isinstance(rendered[0], dict)
                or rendered[0].get("directory")
                not in {directory, "delivery/" + directory}
            ):
                raise ControlError(
                    "Returned clip differs from the requested clip_index"
                )
        for clip in rendered:
            if not re.fullmatch(
                r"(delivery/)?clips/clip-[0-9]{3}", clip.get("directory", "")
            ):
                raise ControlError("Unexpected result directory")
            checksums = clip.get("sha256", {})
            if not isinstance(checksums, dict) or not REQUIRED_CLIP_FILES <= set(
                checksums
            ):
                raise ControlError("Missing result checksums")
            for name, checksum in checksums.items():
                if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or ".." in name:
                    raise ControlError("Unexpected result file name")
                path = Path(output) / clip["directory"] / name
                if not path.is_file():
                    raise ControlError("Missing result file")
                if digest(path) != checksum:
                    raise ControlError("Colab result checksum mismatch")
        print("Colab outputs retrieved and checksums matched.")

    def stop(self):
        if not self.state_path.is_file():
            print("No Colab runtime was allocated by this job.")
            return
        record = self.state()
        if record.get("status") == "stopped":
            return
        client = None
        if record.get("status") in {"uncertain", "creating"} and record.get(
            "notebook_hash"
        ):
            try:
                client = self.client()
                if self.reconcile_assignment(client, record) is None:
                    print("No owned Colab assignment remains.")
                    return
            except Exception:
                raise ControlError(
                    "Allocation outcome is uncertain; check the account's Colab sessions before retrying"
                ) from None
        if not record.get("endpoint"):
            raise ControlError(
                "Allocation outcome is uncertain; check the account's Colab sessions before retrying"
            )
        if client is None:
            client = self.client()
        try:
            client.unassign(record["endpoint"])
        except Exception as error:
            if getattr(getattr(error, "response", None), "status_code", None) != 404:
                raise
        for attempt in range(4):
            if not any(
                item.endpoint == record["endpoint"]
                for item in client.list_assignments()
            ):
                break
            if attempt == 3:
                raise ControlError("GPU release has not been confirmed")
            time.sleep(1 + attempt)
        record = {
            key: value for key, value in record.items() if key not in {"url", "token"}
        }
        record["status"] = "stopped"
        record["released_at"] = round(time.time(), 1)
        # The balance may lag the release by a moment; the difference is a lower bound.
        record["units_after"] = after = compute_units(client)
        write_private(self.state_path, record)
        self.credentials_path.unlink(missing_ok=True)
        print("Owned Colab runtime released; no owned assignment remains.")
        if isinstance(record.get("allocated_at"), (int, float)):
            print(
                f"Runtime held: {record.get('accelerator')} {record.get('shape')} "
                f"for {record['released_at'] - record['allocated_at']:.0f} s",
                flush=True,
            )
        print_units("after release", after)
        before = record.get("units_before")
        if before and after:
            change = before["balance"] - after["balance"]
            if (
                after["assignments"]
                or os.environ.get("CLIPFORGE_SHARED_BALANCE") == "1"
            ):
                # Runtimes running alongside, such as the other ASR part, draw from the same
                # balance, so the difference is not this runtime's own use.
                print(
                    f"Compute units changed account-wide by {change:.2f} "
                    f"({after['assignments']} other runtime(s) still active); "
                    "not this runtime's own use",
                    flush=True,
                )
            else:
                # Separate runs, such as parallel clip renders, also share the balance.
                print(
                    f"Compute units used while this job ran (account-wide): {change:.2f}",
                    flush=True,
                )


def probe(accelerator_name, hold_seconds=0):
    """Ask for one runtime and release it at once, to learn whether the account grants it.

    A refusal allocates nothing. A grant is released immediately and confirmed gone, so the
    check costs seconds of runtime. Used to measure how many runtimes of which kind run at once.
    """
    from colab_cli.client import Accelerator, TooManyAssignmentsError, Variant

    raw = os.environ.get("COLAB_ADC_JSON", "")
    if not raw:
        raise ControlError("COLAB_ADC_JSON is missing; nothing was allocated")
    if accelerator_name not in {"NONE", "T4", "L4", "G4", "A100"}:
        raise ControlError("Unknown accelerator for the probe")
    accelerator = Accelerator(accelerator_name)
    variant = Variant.DEFAULT if accelerator is Accelerator.NONE else Variant.GPU
    shape = colab_shape()
    client = authenticated_client(json.loads(raw))
    before = compute_units(client)
    print_units("before probe", before)
    label = f"{accelerator.value}/{shape.name}"
    notebook_hash = uuid.uuid4()
    allocation_error = None
    try:
        assignment = client.assign(
            notebook_hash, variant=variant, accelerator=accelerator, shape=shape
        )
    except TooManyAssignmentsError:
        print(f"Probe {label}: refused (TooManyAssignmentsError)", flush=True)
        return
    except Exception as error:
        try:
            assignment = lookup_assignment(
                client, notebook_hash, variant, accelerator, shape
            )
        except Exception:
            raise ControlError(
                "Probe allocation outcome is uncertain; check the account's Colab sessions before retrying"
            ) from None
        if assignment is None:
            raise
        allocation_error = error
    try:
        print(f"Probe {label}: granted", flush=True)
        # The account's hourly rate covers every active runtime; the rise over the rate
        # before the probe is this runtime's own rate.
        waited = 0
        while allocation_error is None and waited < hold_seconds:
            time.sleep(min(20, hold_seconds - waited))
            waited += min(20, hold_seconds - waited)
            during = compute_units(client)
            print_units(f"after {waited} s", during)
            if before and during:
                rise = during["hourly_rate"] - before["hourly_rate"]
                print(f"Probe {label}: rate rise {rise:.2f}/h", flush=True)
    finally:
        client.unassign(assignment.endpoint)
        for attempt in range(4):
            if not any(
                item.endpoint == assignment.endpoint
                for item in client.list_assignments()
            ):
                print(f"Probe {label}: released", flush=True)
                break
            if attempt == 3:
                raise ControlError("Probe runtime release has not been confirmed")
            time.sleep(1 + attempt)
    if allocation_error is not None:
        raise allocation_error


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "run", "stop", "doctor", "probe"])
    parser.add_argument(
        "--mode",
        choices=sorted(DEFAULT_POOLS),
        default="asr",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        / "clipforge-colab-control",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        / "clipforge-out",
    )
    args = parser.parse_args()
    controller = Controller(args.state_dir)
    if args.command == "start":
        controller.start(args.mode)
    elif args.command == "run":
        controller.run(args.output)
    elif args.command == "stop":
        controller.stop()
    elif args.command == "probe":
        hold = int(os.environ.get("CLIPFORGE_PROBE_HOLD") or 0)
        if not 0 <= hold <= 300:
            raise ControlError("Probe hold must be 0 to 300 seconds")
        probe(os.environ.get("CLIPFORGE_PROBE_ACCELERATOR") or "T4", hold)
    else:
        raw = os.environ.get("COLAB_ADC_JSON", "")
        if not raw:
            raise ControlError(
                "COLAB_ADC_JSON is missing; no GPU allocation was attempted"
            )
        client = authenticated_client(json.loads(raw))
        print("Verified Google account:", EXPECTED_ACCOUNT)
        print("Visible Colab assignments:", len(client.list_assignments()))
        print_units("now", compute_units(client))
        print("Read-only authentication check completed; no GPU allocated.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = str(error) if isinstance(error, ControlError) else type(error).__name__
        print("Colab control failed: " + detail, file=sys.stderr)
        raise SystemExit(1) from None
