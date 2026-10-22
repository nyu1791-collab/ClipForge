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
            if mode == "pipeline"
            else {
                "request_id": uuid.uuid4().hex,
                "source_ref": os.environ.get("CLIPFORGE_SOURCE_REF", ""),
                "input_tag": "",
            }
        )
        if not re.fullmatch(r"[a-f0-9]{40}", request["source_ref"]):
            raise ControlError("Use an exact ClipForge source SHA")
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
        write_private(self.credentials_path, info)
        record = {
            **request,
            "mode": mode,
            "status": "creating",
            "account": EXPECTED_ACCOUNT,
        }
        write_private(self.state_path, record)
        from colab_cli.client import Accelerator, Variant

        accelerator = Accelerator.NONE if mode == "prepare-cache" else Accelerator.T4
        variant = Variant.DEFAULT if mode == "prepare-cache" else Variant.GPU
        try:
            assignment = client.assign(
                uuid.uuid4(), variant=variant, accelerator=accelerator
            )
        except Exception as error:
            response = getattr(error, "response", None)
            if type(error).__name__ == "TooManyAssignmentsError" or getattr(
                response, "status_code", None
            ) in {400, 401, 403, 412, 429}:
                record["status"] = "stopped"
                record["allocation_refused"] = True
                write_private(self.state_path, record)
                self.credentials_path.unlink(missing_ok=True)
                raise ControlError(
                    "Colab allocation was refused; no other account or accelerator was tried"
                ) from None
            raise
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
        subprocess.run(['git','clone','--no-checkout',{SOURCE_REPO!r},'/content/ClipForge'],
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
        bundles = ReleaseBundles(token)
        if record["mode"] == "pipeline":
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
            complete = (
                manifest.is_file()
                and json.loads(manifest.read_text()).get("status") == "complete"
            )
            if execution_failed or receipt.get("status") != "complete" or not complete:
                raise ControlError(
                    "Colab pipeline failed; available private partial outputs were retrieved"
                )
            for clip in json.loads(manifest.read_text()).get("rendered", []):
                if not re.fullmatch(r"clips/clip-[0-9]{3}", clip.get("directory", "")):
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
        write_private(self.state_path, record)
        self.credentials_path.unlink(missing_ok=True)
        print("Owned Colab runtime released; no owned assignment remains.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "run", "stop", "doctor"])
    parser.add_argument(
        "--mode",
        choices=["pipeline", "verify-gpu", "prepare-cache"],
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
    else:
        raw = os.environ.get("COLAB_ADC_JSON", "")
        if not raw:
            raise ControlError(
                "COLAB_ADC_JSON is missing; no GPU allocation was attempted"
            )
        client = authenticated_client(json.loads(raw))
        print("Verified Google account:", EXPECTED_ACCOUNT)
        print("Visible Colab assignments:", len(client.list_assignments()))
        print("Read-only authentication check completed; no GPU allocated.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = str(error) if isinstance(error, ControlError) else type(error).__name__
        print("Colab control failed: " + detail, file=sys.stderr)
        raise SystemExit(1) from None
