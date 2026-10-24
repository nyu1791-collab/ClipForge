#!/usr/bin/env python3
"""Dedicated Colab worker; receives the GitHub token through a private file.

asr: one audio part is recognised with Parakeet in the runtime's own Python. NeMo and the
model are fetched in the background from the start, alongside the input download, and the
project environment is never installed. audio-stage: one reviewed clip (or every clip) is
rendered on a CPU runtime from the verified analysis.
"""

import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from colab_bundle import ReleaseBundles, digest

ROOT = Path("/content/ClipForge")
WORK = Path("/content/clipforge-job")
MAX_PART_BYTES = 2 * 1024**3


@contextmanager
def timed(timings, name):
    # Records elapsed seconds even when the stage fails; only the stage name is stored.
    started = time.monotonic()
    try:
        yield
    finally:
        timings[name] = round(time.monotonic() - started, 2)


def read_setup_timings(path):
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return {}
    timings = {}
    for line in lines:
        name, separator, seconds = line.partition("\t")
        if not separator or not name:
            continue
        try:
            timings["setup." + name] = float(seconds)
        except ValueError:
            continue
    return timings


def publish_result(bundles, payload, output, report, timings):
    # Returns False if publication failed; the control plane then reports a failure.
    receipt = output / "request-receipt.json"
    if not receipt.is_file():
        receipt.write_text(
            json.dumps(
                {
                    "request_id": payload["request_id"],
                    "source_ref": payload["source_ref"],
                    "status": "failed",
                    "rendered_clips": 0,
                }
            )
        )
    (output / "gpu-receipt.json").write_text(json.dumps(report))
    try:
        returned = json.loads(receipt.read_text())
        if isinstance(returned.get("sha256"), dict):
            returned["sha256"]["gpu-receipt.json"] = digest(output / "gpu-receipt.json")
            receipt.write_text(json.dumps(returned))
        with (
            timed(timings, "result_publish"),
            tempfile.TemporaryDirectory(prefix="clipforge-result-upload-") as work,
        ):
            bundles.publish(payload["input_tag"], output, work, payload["request_id"])
    except Exception as error:
        report["result_error_type"] = type(error).__name__
        return False
    return True


def command(arguments, environment, name, timeout=4800):
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    path = WORK / (name + ".log")
    path.write_text(result.stdout + result.stderr)
    path.chmod(0o600)
    if result.returncode:
        raise RuntimeError(name + " failed")
    return result.stdout


def host_info():
    """CPU cores and memory of the assigned runtime (standard or high-memory shape)."""
    pages = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "cpu_count": len(os.sched_getaffinity(0)),
        "cpu_model": model,
        "memory_gib": round(pages / 2**30, 1),
    }


def worker_error(path):
    # Only the error type and the worker's own bounded message are kept; never logs or paths.
    try:
        record = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("error_type"), str):
        return None
    message = record.get("message")
    if not isinstance(message, str):
        message = ""
    return {"error_type": record["error_type"], "message": message[:300]}


def gpu_info(expected="T4"):
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("NVIDIA GPU detection failed")
    devices = []
    for line in result.stdout.splitlines():
        name, driver, memory = [v.strip() for v in line.split(",")]
        if not name.endswith(expected):
            raise RuntimeError("Requested GPU was not assigned; no fallback is allowed")
        devices.append({"name": name, "driver": driver, "memory_mib": int(memory)})
    if not devices or ctypes.CDLL("libcuda.so.1").cuInit(0) != 0:
        raise RuntimeError("CUDA driver initialization failed")
    return {"devices": devices, "cuda_driver_initialized": True}


def log_tail(path, lines=20):
    """The last ASCII lines of a private log, so a failure can be diagnosed.

    Lines with other characters are dropped, so recognised Japanese text never leaves the
    runtime; package and CUDA errors are ASCII.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    kept = [line[:300] for line in text.splitlines() if line.isascii() and line.strip()]
    return kept[-lines:]


def asr_environment():
    """This Python's environment for NeMo, without the GitHub token or project paths."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GH_TOKEN", "GITHUB_TOKEN", "COLAB_ADC_JSON"}
        and not key.startswith(("VIRTUAL_ENV", "PYTHON"))
    }
    environment.update(
        PYTHONPATH=str(ROOT / "src"),
        PYTHONUNBUFFERED="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_PROGRESS_BARS="1",
    )
    return environment


PART_REQUEST_FIELDS = {
    "request_id",
    "source_ref",
    "audio_sha256",
    "audio_bytes",
    "language",
    "asr_part",
}


def download_part(bundles, payload, directory):
    """The part's request and 16 kHz WAV from its private release, checked against each other."""
    release = bundles.release(payload["input_tag"])
    assets = {asset["name"]: asset for asset in (release or {}).get("assets", [])}
    if "request.json" not in assets or "audio.wav" not in assets:
        raise RuntimeError("ASR part input is missing")
    directory.mkdir(mode=0o700)
    bundles.download_asset(assets["request.json"], directory / "request.json", 65536)
    request = json.loads((directory / "request.json").read_text())
    if (
        not isinstance(request, dict)
        or set(request) != PART_REQUEST_FIELDS
        or request["request_id"] != payload["request_id"]
        or request["source_ref"] != payload["source_ref"]
        or not isinstance(request["asr_part"], dict)
        or not re.fullmatch(r"[a-z]{2,3}", str(request["language"]))
        or assets["audio.wav"]["size"] != request["audio_bytes"]
    ):
        raise RuntimeError("ASR part request identity mismatch")
    audio = directory / "audio.wav"
    bundles.download_asset(assets["audio.wav"], audio, MAX_PART_BYTES)
    if digest(audio) != request["audio_sha256"]:
        raise RuntimeError("ASR part audio checksum mismatch")
    return request, audio


def recognise_part(bundles, payload, output, report, timings):
    environment = asr_environment()
    python = sys.executable
    # NeMo and the model download while the input does; recognition waits on its lock.
    prefetch_log = WORK / "prefetch.log"
    with prefetch_log.open("w") as log:
        prefetch = subprocess.Popen(
            [python, "-m", "clipforge.asr", "prefetch"],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        report["stage"] = "private-input"
        with timed(timings, "input"):
            request, audio = download_part(bundles, payload, WORK / "input")
        report["stage"] = "prefetch"
        with timed(timings, "prefetch_wait"):
            if prefetch.wait(timeout=1800):
                report["log_tail"] = log_tail(prefetch_log)
                raise RuntimeError("NeMo or the Parakeet model could not be fetched")
        report["stage"] = "recognise"
        with timed(timings, "recognise"):
            try:
                command(
                    [
                        python,
                        "-m",
                        "clipforge.asr",
                        "recognise",
                        "--input",
                        str(audio),
                        "--output",
                        str(output / "transcript.json"),
                        "--language",
                        request["language"],
                    ],
                    environment,
                    "recognise",
                    timeout=3600,
                )
            except RuntimeError:
                report["log_tail"] = log_tail(WORK / "recognise.log")
                raise
    finally:
        if prefetch.poll() is None:
            prefetch.kill()
    manifest = {
        "status": "asr_part_complete",
        "asr_part": request["asr_part"],
        "audio_sha256": request["audio_sha256"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest))
    (output / "request-receipt.json").write_text(
        json.dumps(
            {
                "request_id": payload["request_id"],
                "source_ref": payload["source_ref"],
                "workflow_run": payload["workflow_run"],
                "status": "asr_part_complete",
                "rendered_clips": 0,
                "asr_part": request["asr_part"],
                "sha256": {
                    name: digest(output / name)
                    for name in ("manifest.json", "transcript.json")
                },
            }
        )
    )
    return manifest["status"]


def render_stage(payload, output, report, timings, environment):
    # One reviewed clip (or every clip) is rendered from the verified analysis artifact;
    # the worker itself checks the previous run and its identity.
    report["stage"] = "asset-setup"
    with timed(timings, "setup"):
        try:
            command(["bash", "tools/colab_setup.sh"], environment, "setup")
        finally:
            timings.update(read_setup_timings(WORK / "setup-timings.tsv"))
    report["stage"] = "render"
    stage = payload["stage"]
    review = WORK / "review.json"
    review.write_text(stage["review"], encoding="utf-8")
    arguments = [
        str(ROOT / ".venv/bin/python"),
        "tools/run_audio_stage.py",
        "--stage",
        "render",
        "--previous-run",
        stage["previous_run"],
        "--previous-request",
        stage["previous_request"],
        "--request-id",
        payload["request_id"],
        "--source-ref",
        payload["source_ref"],
        "--review-file",
        str(review),
        "--output",
        str(output),
    ]
    if stage.get("clip_index"):
        arguments += ["--clip-index", stage["clip_index"]]
    if stage.get("config_file"):
        if not re.fullmatch(r"config\.[a-z0-9-]+\.example\.json", stage["config_file"]):
            raise RuntimeError("Invalid configuration file")
        arguments += ["--config", stage["config_file"]]
    stage_env = {**environment, "GH_TOKEN": payload["github_token"]}
    if payload.get("youtube_cookies_b64"):
        stage_env["YOUTUBE_COOKIES_B64"] = payload["youtube_cookies_b64"]
    invocation = 'source tools/colab_cache_env.sh && exec "$@"'
    try:
        with timed(timings, "render"):
            command(
                ["bash", "-c", invocation, "clipforge-stage", *arguments],
                stage_env,
                "render",
            )
    except RuntimeError:
        error = worker_error(output / "stage-error.json")
        if error is not None:
            report["worker_error"] = error
        raise
    return json.loads((output / "manifest.json").read_text())["status"]


def main():
    os.umask(0o077)
    payload_path = Path("/content/.clipforge-job.json")
    payload = json.loads(payload_path.read_text())
    payload_path.unlink()
    mode = payload["mode"]
    if mode not in {"asr", "audio-stage"}:
        raise RuntimeError("Invalid job mode")
    if not re.fullmatch(r"[a-f0-9]{40}", payload["source_ref"]):
        raise RuntimeError("Invalid source revision")
    if payload["input_tag"] != "clipforge-input-" + payload["request_id"]:
        raise RuntimeError("Input identity mismatch")
    WORK.mkdir(mode=0o700)
    bundles = ReleaseBundles(payload["github_token"])
    accelerator = payload.get("accelerator", "T4")
    timings = {}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GEMINI_API_KEY",
            "COLAB_ADC_JSON",
            "YOUTUBE_COOKIES_B64",
        }
    }
    # Clip rendering may run on a CPU runtime; it needs no CUDA.
    environment.update(
        {
            "CLIPFORGE_CPU_ONLY": "1" if accelerator == "NONE" else "0",
            "CLIPFORGE_REQUIRE_CUDA": "0",
            "CLIPFORGE_SETUP_TIMINGS": str(WORK / "setup-timings.tsv"),
            "CLIPFORGE_SOURCE_REF": payload["source_ref"],
            "GITHUB_REPOSITORY": "nyu1791-collab/ai-model-workers",
            "GITHUB_RUN_ID": payload["workflow_run"],
            "PYTHONUNBUFFERED": "1",
        }
    )
    output = WORK / "out"
    output.mkdir()
    report = {
        "mode": mode,
        "status": "failed",
        "source_ref": payload["source_ref"],
        "human_review_performed": False,
        "timings": timings,
    }
    failed = False
    try:
        report["stage"] = "hardware-check"
        if accelerator != "NONE":
            report["gpu"] = gpu_info(accelerator)
        report["host"] = host_info()
        if mode == "asr":
            report["status"] = recognise_part(bundles, payload, output, report, timings)
        else:
            report["status"] = render_stage(
                payload, output, report, timings, environment
            )
        report["stage"] = "complete"
    except Exception as error:
        failed = True
        report["error_type"] = type(error).__name__
    finally:
        if not publish_result(bundles, payload, output, report, timings):
            failed = True
    if failed:
        raise RuntimeError("Colab job failed; available private output was preserved")
    print(
        "Dedicated Colab job completed; release of the runtime is handled by Actions."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never emit provider bodies, signed URLs, keys, or the private payload.
        print("Colab worker failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
