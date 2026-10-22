#!/usr/bin/env python3
"""Dedicated Colab worker; receives GitHub/Gemini secrets through a private file."""

import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from colab_bundle import ReleaseBundles, REPOSITORY
from colab_cache import configuration, mark_restored, release_parts, release_tag

ROOT = Path("/content/ClipForge")
WORK = Path("/content/clipforge-job")


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
    # Results are published before the dependency cache so the control plane can
    # release the T4 and retrieve them early. Returns False if publication failed.
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
        with (
            timed(timings, "result_publish"),
            tempfile.TemporaryDirectory(prefix="clipforge-result-upload-") as work,
        ):
            bundles.publish(
                payload["input_tag"], output, work, "result", payload["request_id"]
            )
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


def verify_asr(environment):
    script = r"""
import json, tempfile, wave
from pathlib import Path
from faster_whisper import WhisperModel
from clipforge.hardware import nvenc_available
from clipforge.whisper_cache import model_path
model = WhisperModel(model_path('small'), device='cuda', compute_type='float16')
assert model.model.device == 'cuda'
assert model.model.compute_type == 'float16'
with tempfile.TemporaryDirectory() as temporary:
    path = Path(temporary) / 'synthetic.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(bytes(64000))
    segments, metadata = model.transcribe(str(path), language='en', vad_filter=False, beam_size=1)
    count = sum(1 for _ in segments)
print('CLIPFORGE_ASR_VERIFIED:' + json.dumps({'device': model.model.device,
      'compute_type': model.model.compute_type, 'synthetic_asr_completed': True,
      'segments': count, 'nvenc_available': nvenc_available(),
      'real_video_quality_assessed': False, 'gemini_requests': 0}))
"""
    invocation = 'source tools/colab_cache_env.sh && source tools/colab_cuda_env.sh && exec .venv/bin/python -c "$1"'
    result = command(
        ["bash", "-c", invocation, "clipforge-verify", script],
        environment,
        "verify-asr",
    )
    for line in result.splitlines():
        if line.startswith("CLIPFORGE_ASR_VERIFIED:"):
            return json.loads(line.split(":", 1)[1])
    raise RuntimeError("GPU ASR verification receipt is missing")


def main():
    os.umask(0o077)
    payload_path = Path("/content/.clipforge-job.json")
    payload = json.loads(payload_path.read_text())
    payload_path.unlink()
    mode = payload["mode"]
    if mode not in {"pipeline", "audio-stage", "verify-gpu", "prepare-cache"}:
        raise RuntimeError("Invalid job mode")
    if not re.fullmatch(r"[a-f0-9]{40}", payload["source_ref"]):
        raise RuntimeError("Invalid source revision")
    WORK.mkdir(mode=0o700)
    bundles = ReleaseBundles(payload["github_token"])
    # These paths contain public assets only. No Google OAuth credentials are sent here.
    accelerator = payload.get("accelerator", "T4")
    cpu_only = mode == "prepare-cache" or accelerator == "NONE"
    os.environ["CLIPFORGE_CPU_ONLY"] = "1" if cpu_only else "0"
    cache_profile = "cpu" if cpu_only else "gpu"
    if mode == "prepare-cache":
        cache_profile = payload.get("cache_profile", "gpu")
    if cache_profile not in {"cpu", "gpu"}:
        raise RuntimeError("Invalid cache profile: use cpu or gpu")
    # 2026-10-22 Colab CPU high-RAM, 3 parallel jobs: fresh setup took 28s;
    # restoring the 602 MB public cache plus setup took 30-72s, before publication.
    use_public_cache = cache_profile != "cpu" or mode == "prepare-cache"
    os.environ["CLIPFORGE_CACHE_PROFILE"] = cache_profile
    # Releases contain the assets themselves; Drive's nested tar is not used here.
    os.environ["CLIPFORGE_PERSISTENT_CACHE"] = ""
    key, cache_root, _ = configuration()
    cache_tag = release_tag(key)
    parts = release_parts()
    timings = {}
    environment = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GEMINI_API_KEY", "COLAB_ADC_JSON"):
        environment.pop(name, None)
    # Clip rendering may run on a CPU runtime; it needs no CUDA and no Whisper model.
    environment.update(
        {
            "CLIPFORGE_PERSISTENT_CACHE": "",
            "CLIPFORGE_REQUIRE_CUDA": "0" if cpu_only or mode == "audio-stage" else "1",
            "CLIPFORGE_CPU_ONLY": "1" if cpu_only else "0",
            "CLIPFORGE_SKIP_RUNNER_ASSET": "1",
            "CLIPFORGE_SKIP_MODEL_PREPARE": "1"
            if mode in {"pipeline", "audio-stage"}
            else "0",
            "CLIPFORGE_PREPARE_ONLY": "1" if mode == "prepare-cache" else "0",
            "CLIPFORGE_OFFLINE": "1" if payload.get("offline") else "0",
            "CLIPFORGE_SETUP_TIMINGS": str(WORK / "setup-timings.tsv"),
            "CLIPFORGE_SETUP_FETCHED": str(WORK / "setup-fetched"),
            "CLIPFORGE_SOURCE_REF": payload["source_ref"],
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_RUN_ID": payload["workflow_run"],
            "GITHUB_OUTPUT": str(WORK / "prepare-output"),
            "PYTHONUNBUFFERED": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        }
    )
    output = WORK / "out"
    output.mkdir()
    report = {
        "mode": mode,
        "status": "failed",
        "source_ref": payload["source_ref"],
        "public_cache_restored": False,
        "gemini_requests_in_verification": 0,
        "human_review_performed": False,
        "timings": timings,
    }
    if not use_public_cache:
        report["public_cache_restored"] = "skipped_cpu_profile"
        report["public_cache_saved"] = "skipped_cpu_profile"
    failed = False
    cached = False
    setup_complete = False
    try:
        if use_public_cache:
            report["stage"] = "cache-restore"
            with timed(timings, "cache_restore"):
                try:
                    cached = bundles.restore(
                        cache_tag, cache_root, "assets", key, parts=parts
                    )
                    if cached:
                        mark_restored(key, cache_root)
                except Exception as error:
                    report["cache_restore_error"] = type(error).__name__
                    cached = False
                    # Direct extraction can leave partial assets; setup must fetch them afresh.
                    for name in parts:
                        path = cache_root / name
                        if path.is_symlink() or path.is_file():
                            path.unlink()
                        elif path.exists():
                            shutil.rmtree(path)
                    for name in (".restored", ".saved-inventory"):
                        (cache_root / name).unlink(missing_ok=True)
                report["public_cache_restored"] = cached
        report["stage"] = "hardware-check"
        if not cpu_only:
            report["gpu"] = gpu_info(accelerator)
        report["host"] = host_info()
        report["stage"] = "asset-setup"
        with timed(timings, "setup"):
            try:
                command(["bash", "tools/colab_setup.sh"], environment, "setup")
                setup_complete = True
            finally:
                timings.update(read_setup_timings(WORK / "setup-timings.tsv"))
        if mode == "pipeline":
            report["stage"] = "private-input"
            tag = payload["input_tag"]
            if tag != "clipforge-input-" + payload["request_id"]:
                raise RuntimeError("Input identity mismatch")
            python = str(ROOT / ".venv/bin/python")
            prepare_env = {**environment, "GH_TOKEN": payload["github_token"]}
            arguments = ["--tag", tag, "--directory", str(WORK / "input")]
            with timed(timings, "prepare"):
                command(
                    [python, "tools/run_request.py", "prepare", *arguments],
                    prepare_env,
                    "prepare",
                )
            arguments = ["tools/run_request.py", "run", *arguments]
            # API secrets are withheld from dependency setup and cache handling.
            run_env = environment.copy()
            if payload.get("gemini_api_key"):
                run_env["GEMINI_API_KEY"] = payload["gemini_api_key"]
            report["stage"] = "pipeline"
            invocation = 'source tools/colab_cache_env.sh && source tools/colab_cuda_env.sh && exec "$@"'
            with timed(timings, "pipeline"):
                command(
                    [
                        "bash",
                        "-c",
                        invocation,
                        "clipforge-run",
                        python,
                        *arguments,
                        "--output",
                        str(output),
                    ],
                    run_env,
                    "pipeline",
                )
            report["status"] = json.loads((output / "manifest.json").read_text())[
                "status"
            ]
        elif mode == "audio-stage":
            # One reviewed clip (or every clip) is rendered from the verified analysis
            # artifact; the worker itself checks the previous run and its identity.
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
            stage_env = {**environment, "GH_TOKEN": payload["github_token"]}
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
            report["status"] = json.loads((output / "manifest.json").read_text())[
                "status"
            ]
        elif mode == "verify-gpu":
            # The child verification needs the same cache/library paths as the worker.
            report["stage"] = "cuda-asr-verification"
            report["asr"] = verify_asr(environment)
            report["status"] = "complete"
        else:
            report["status"] = "complete"
        report["stage"] = "complete"
    except Exception as error:
        failed = True
        report["error_type"] = type(error).__name__
    finally:
        # Results first (partial results included), then the dependency cache.
        if mode in {"pipeline", "audio-stage"} and not publish_result(
            bundles, payload, output, report, timings
        ):
            failed = True
        # Only clip 1 (or an unsplit render) may save the audio-stage cache, even on a miss.
        # A restored pipeline/audio-stage cache is rebuilt only when setup fetched new assets.
        fetched = (WORK / "setup-fetched").is_file()
        report["setup_fetched"] = fetched
        first = (payload.get("stage") or {}).get("clip_index") in (None, "1")
        if not use_public_cache:
            report["public_cache_saved"] = "skipped_cpu_profile"
        elif not setup_complete:
            report["public_cache_saved"] = "skipped_setup_failed"
        elif mode == "audio-stage" and not first:
            report["public_cache_saved"] = "skipped_parallel"
        elif mode in {"pipeline", "audio-stage"} and cached and not (fetched and first):
            report["public_cache_saved"] = "skipped_restored"
        else:
            try:
                with (
                    timed(timings, "cache_publish"),
                    tempfile.TemporaryDirectory(
                        prefix="clipforge-cache-upload-"
                    ) as work,
                ):
                    bundles.publish(
                        cache_tag, cache_root, work, "assets", key, parts=parts
                    )
                report["public_cache_saved"] = True
            except Exception as error:
                report["public_cache_saved"] = False
                report["cache_error_type"] = type(error).__name__
        Path("/content/clipforge-report.json").write_text(json.dumps(report))
        (output / "gpu-receipt.json").write_text(json.dumps(report))
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
