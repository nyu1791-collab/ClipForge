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


def colab_shape():
    """The runtime shape named by CLIPFORGE_COLAB_SHAPE: standard (default) or high-ram."""
    from colab_cli.client import Shape

    shapes = {"standard": Shape.STANDARD, "high-ram": Shape.HIGH_RAM}
    name = os.environ.get("CLIPFORGE_COLAB_SHAPE") or "standard"
    if name not in shapes:
        raise ControlError("Unknown Colab shape: use standard or high-ram")
    return shapes[name]


# Clip rendering is CPU work (x264), so it tries the cheapest runtime first and moves on only
# when the account refuses one more of that kind. Measured 2026-10-22: at most two T4 and two
# L4 at once, counted per kind; three CPU high-ram and two CPU standard were granted together.
# Rates then: CPU high-ram 0.26, T4 1.07, T4 high-ram 1.27, L4 1.54 compute units an hour.
DEFAULT_POOLS = {"audio-stage": "CPU:high-ram,CPU:standard,T4:high-ram,L4"}


def colab_pool(mode):
    """Ordered (accelerator, shape) candidates; CLIPFORGE_COLAB_POOL overrides the default."""
    from colab_cli.client import Accelerator, Shape

    raw = os.environ.get("CLIPFORGE_COLAB_POOL") or DEFAULT_POOLS.get(mode)
    if not raw:
        accelerator = Accelerator.NONE if mode == "prepare-cache" else Accelerator.T4
        return [(accelerator, colab_shape())]
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
    }
    if (
        stage["audio_stage"] != "render"
        or not re.fullmatch(r"[0-9]{1,20}", stage["previous_run"])
        or not re.fullmatch(r"[a-f0-9]{32}", stage["previous_request"])
        or (
            stage["clip_index"] is not None
            and not re.fullmatch(r"[1-9][0-9]?", stage["clip_index"])
        )
    ):
        raise ControlError("Invalid audio stage request")
    review = Path(os.environ.get("REVIEW_FILE", "")).read_text(encoding="utf-8")
    if len(review.encode()) > 32768:
        raise ControlError("Review is too large")
    json.loads(review)
    stage["review"] = review
    return stage


class Controller:
    def __init__(self, root):
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.credentials_path = self.root / "adc.json"

    def state(self):
        return json.loads(self.state_path.read_text())

    def client(self):
        return authenticated_client(json.loads(self.credentials_path.read_text()))

    def start(self, mode="pipeline"):
        request = (
            identifiers()
            if mode in {"pipeline", "audio-stage"}
            else {
                "request_id": uuid.uuid4().hex,
                "source_ref": os.environ.get("CLIPFORGE_SOURCE_REF", ""),
                "input_tag": "",
            }
        )
        if not re.fullmatch(r"[a-f0-9]{40}", request["source_ref"]):
            raise ControlError("Use an exact ClipForge source SHA")
        # The high-memory shape gives 8 CPU cores instead of 2 for the CPU-bound setup,
        # decompression and encoding around the GPU work, at a higher hourly rate.
        pool = colab_pool(mode)
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
        existing = {item.endpoint for item in client.list_assignments()}
        units = compute_units(client)
        print_units("before allocation", units)
        write_private(self.credentials_path, info)
        record = {
            **request,
            "mode": mode,
            "status": "creating",
            "account": EXPECTED_ACCOUNT,
            "units_before": units,
        }
        write_private(self.state_path, record)
        from colab_cli.client import Accelerator, Variant

        refused = []
        for accelerator, shape in pool:
            variant = (
                Variant.DEFAULT if accelerator is Accelerator.NONE else Variant.GPU
            )
            record.update(accelerator=accelerator.value, shape=shape.name)
            write_private(self.state_path, record)
            try:
                assignment = client.assign(
                    uuid.uuid4(), variant=variant, accelerator=accelerator, shape=shape
                )
                break
            except Exception as error:
                response = getattr(error, "response", None)
                if type(error).__name__ == "TooManyAssignmentsError" or getattr(
                    response, "status_code", None
                ) in {400, 401, 403, 412, 429}:
                    refused.append(f"{accelerator.value}/{shape.name}")
                    print(f"Colab refused {refused[-1]}", flush=True)
                    continue
                # The reply may be lost after Colab assigned a runtime. A runtime that
                # appeared meanwhile is recorded as this job's so the release step frees it.
                try:
                    appeared = [
                        item.endpoint
                        for item in client.list_assignments()
                        if item.endpoint not in existing
                    ]
                except Exception:
                    appeared = None
                record["allocation_error"] = type(error).__name__
                if appeared == []:
                    record["status"] = "stopped"
                    write_private(self.state_path, record)
                    self.credentials_path.unlink(missing_ok=True)
                elif appeared and len(appeared) == 1:
                    record.update(status="allocated", endpoint=appeared[0])
                    write_private(self.state_path, record)
                raise
        else:
            record.update(status="stopped", allocation_refused=refused)
            write_private(self.state_path, record)
            self.credentials_path.unlink(missing_ok=True)
            raise ControlError(
                "Colab refused every runtime in the pool; no other account was tried"
            )
        # Persist ownership immediately, before kernel setup or any job execution.
        record.update(
            status="allocated",
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
        if record["mode"] == "pipeline" and not os.environ.get("GEMINI_API_KEY"):
            raise ControlError("GEMINI_API_KEY is missing")
        payload = {
            **record,
            "github_token": token,
            "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
            "workflow_run": os.environ.get("GITHUB_RUN_ID", "manual"),
            "offline": os.environ.get("CLIPFORGE_OFFLINE") == "1",
        }
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
        execution_failed = False
        try:
            contents.upload(str(private_payload), "/content/.clipforge-job.json")
            source = record["source_ref"]
            bootstrap = f"""
import json, pathlib, runpy, subprocess, sys

def _clipforge_bootstrap():
    payload_path = pathlib.Path('/content/.clipforge-job.json')
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
            except Exception:
                execution_failed = True
        finally:
            private_payload.unlink(missing_ok=True)
            runtime.stop()
        release_error = None
        if record["mode"] in {"pipeline", "audio-stage"}:
            # The results are already published in the Release, so the T4 is freed before
            # downloading them. stop() is idempotent; the workflow's final stop step retries
            # if this release is not confirmed.
            try:
                self.stop()
            except Exception as error:
                release_error = error
        try:
            self.collect(record, output, execution_failed, token, remote_state)
        finally:
            self.report_stages(output)
            if release_error is not None:
                print(
                    "Early Colab release failed: " + type(release_error).__name__,
                    file=sys.stderr,
                )
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
                + str(receipt.get("timings", {})),
                flush=True,
            )
            return

    def collect(self, record, output, execution_failed, token, remote_state):
        bundles = ReleaseBundles(token)
        if record["mode"] in {"pipeline", "audio-stage"}:
            bundles.restore(
                record["input_tag"],
                output,
                "result",
                record["request_id"],
                required=True,
            )
            receipt = json.loads((Path(output) / "request-receipt.json").read_text())
            if (
                receipt.get("request_id") != record["request_id"]
                or receipt.get("source_ref") != record["source_ref"]
            ):
                raise ControlError(
                    "Returned GPU result identity differs from the request"
                )
            manifest = Path(output) / "manifest.json"
            # Audio analysis ends once topics are allocated, one reviewed clip at
            # clip_rendered, and full video jobs with every clip rendered.
            if record["mode"] == "audio-stage":
                expected = (
                    "clip_rendered" if record["stage"]["clip_index"] else "complete"
                )
            elif receipt.get("audio_source_url"):
                expected = "topics_allocated"
            else:
                expected = "complete"
            complete = (
                manifest.is_file()
                and json.loads(manifest.read_text()).get("status") == expected
            )
            if execution_failed or receipt.get("status") != expected or not complete:
                raise ControlError(
                    "Colab pipeline failed; available private partial outputs were retrieved"
                )
            for name, checksum in receipt.get("sha256", {}).items():
                if (
                    not re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){0,3}", name)
                    or ".." in name
                ):
                    raise ControlError("Unexpected result file name")
                if digest(Path(output) / name) != checksum:
                    raise ControlError("GPU result checksum mismatch")
            for clip in json.loads(manifest.read_text()).get("rendered", []):
                # Video jobs write clips/ at the top; audio-first stages write delivery/clips/.
                if not re.fullmatch(
                    r"(delivery/)?clips/clip-[0-9]{3}", clip.get("directory", "")
                ):
                    raise ControlError("Unexpected result directory")
                checksums = clip.get("sha256", {})
                if set(checksums) != {"video.mp4", "captions.ass", "captions.srt"}:
                    raise ControlError("Missing result checksums")
                for name, checksum in checksums.items():
                    if digest(Path(output) / clip["directory"] / name) != checksum:
                        raise ControlError("GPU result checksum mismatch")
            print(
                "GPU pipeline outputs retrieved and checksums matched; human review is not performed."
            )
        else:
            # Verification/preparation reports contain only public hardware/cache status.
            fresh = next(
                (
                    item
                    for item in self.client().list_assignments()
                    if item.endpoint == record["endpoint"]
                ),
                None,
            )
            if fresh is None:
                raise ControlError(
                    "Colab ended before its verification report could be retrieved"
                )
            remote_state.token = fresh.runtime_proxy_info.token
            remote_state.url = fresh.runtime_proxy_info.url
            from colab_cli.contents import ContentsClient

            contents = ContentsClient(remote_state)
            report_path = self.root / "remote-report.json"
            contents.download("/content/clipforge-report.json", str(report_path))
            report = json.loads(report_path.read_text())
            if (report.get("mode"), report.get("source_ref")) != (
                record["mode"],
                record["source_ref"],
            ):
                raise ControlError(
                    "Returned Colab report identity differs from the request"
                )
            Path(output).mkdir(parents=True, exist_ok=True)
            write_private(Path(output) / "colab-report.json", report)
            if execution_failed or report.get("status") != "complete":
                raise ControlError(
                    "Colab verification/preparation failed; its private report was saved"
                )
            print("Colab verification/preparation completed; report saved.")

    def stop(self):
        if not self.state_path.is_file():
            print("No Colab runtime was allocated by this job.")
            return
        record = self.state()
        if record.get("status") == "stopped":
            return
        if not record.get("endpoint"):
            raise ControlError(
                "Allocation outcome is uncertain; check the account's Colab sessions before retrying"
            )
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
        # The balance may lag the release by a moment; the difference is a lower bound.
        record["units_after"] = after = compute_units(client)
        write_private(self.state_path, record)
        self.credentials_path.unlink(missing_ok=True)
        print("Owned Colab runtime released; no owned assignment remains.")
        print_units("after release", after)
        before = record.get("units_before")
        if before and after:
            print(
                f"Compute units used by this job: {before['balance'] - after['balance']:.2f}",
                flush=True,
            )


def probe(accelerator_name, hold_seconds=0):
    """Ask for one runtime and release it at once, to learn whether the account grants it.

    A refusal allocates nothing. A grant is released immediately and confirmed gone, so the
    check costs seconds of runtime. Used to measure how many runtimes of which kind run at once.
    """
    from colab_cli.client import Accelerator, Variant

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
    try:
        assignment = client.assign(
            uuid.uuid4(), variant=variant, accelerator=accelerator, shape=shape
        )
    except Exception as error:
        print(f"Probe {label}: refused ({type(error).__name__})", flush=True)
        return
    try:
        print(f"Probe {label}: granted", flush=True)
        # The account's hourly rate covers every active runtime; the rise over the rate
        # before the probe is this runtime's own rate.
        waited = 0
        while waited < hold_seconds:
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


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "run", "stop", "doctor", "probe"])
    parser.add_argument(
        "--mode",
        choices=["pipeline", "audio-stage", "verify-gpu", "prepare-cache"],
        default="pipeline",
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
