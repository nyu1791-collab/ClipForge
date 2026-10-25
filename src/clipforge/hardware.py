"Resolve hardware and the requested model cache; no inference at import time."

import ctypes
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from .storage import PipelineError


def cpu_threads(settings):
    if settings.threads:
        return settings.threads
    return _available_cpu_threads()


def encode_threads(settings):
    """Rendering uses this machine's available cores, independently of ASR."""
    return settings.encode_threads or _available_cpu_threads()


def _available_cpu_threads():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def asr_options(settings):
    # CTranslate2, rather than torch/nvidia-smi, is the inference backend.
    import ctranslate2

    device = settings.whisper_device
    if device in {"auto", "cuda"}:
        try:
            supported = ctranslate2.get_supported_compute_types("cuda")
            available = (
                ctranslate2.get_cuda_device_count() > 0 and "float16" in supported
            )
            if available:
                ctypes.CDLL("libcublas.so.12")
                ctypes.CDLL("libcudnn.so.9")
        except (OSError, RuntimeError, ValueError):
            available = False
        if device == "cuda" and not available:
            raise PipelineError(
                "CUDA/float16 or cuBLAS/cuDNN is unavailable; GPU execution required"
            )
        device = "cuda" if available else "cpu"
    compute = settings.whisper_compute_type
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    try:
        supported = ctranslate2.get_supported_compute_types(device)
    except (RuntimeError, ValueError):
        raise PipelineError(
            "CTranslate2 could not initialize the selected device"
        ) from None
    if compute not in supported:
        raise PipelineError(f"CTranslate2 does not support {device}/{compute}")
    return {
        "device": device,
        "compute_type": compute,
        "cpu_threads": cpu_threads(settings),
    }


@lru_cache(maxsize=1)
def nvenc_available():
    # An encoder listing alone does not prove a working driver/NVENC session.
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                # Turing H.264 NVENC requires width >=145; a 128px probe rejects T4.
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=30",
                "-frames:v",
                "1",
                "-an",
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p1",
                "-rc",
                "vbr",
                "-cq",
                "20",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def video_options(settings, quality):
    encoder = settings.video_encoder
    if encoder in {"auto", "h264_nvenc"}:
        available = nvenc_available()
        if encoder == "h264_nvenc" and not available:
            raise PipelineError(
                "FFmpeg h264_nvenc is unavailable; NVENC execution required"
            )
        encoder = "h264_nvenc" if available else "libx264"
    if encoder == "h264_nvenc":
        options = ["-preset", "p1", "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    else:
        options = ["-preset", "veryfast", "-crf", str(quality)]
    return [
        "-c:v",
        encoder,
        *options,
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(encode_threads(settings)),
    ]


def model_path(model, revision=None):
    if Path(model).is_dir():
        return model
    from faster_whisper.utils import download_model
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        path = download_model(model, revision=revision, local_files_only=True)
        if all(
            (Path(path) / name).is_file()
            for name in ("model.bin", "config.json", "tokenizer.json")
        ):
            print("Whisper model found in cache; Hub download skipped.", flush=True)
            return path
    except LocalEntryNotFoundError:
        pass
    if (
        os.environ.get("CLIPFORGE_OFFLINE") == "1"
        or os.environ.get("HF_HUB_OFFLINE") == "1"
    ):
        raise PipelineError(
            "requested Whisper model is not cached; offline mode forbids downloading"
        )
    print(
        "Requested Whisper model is missing; downloading it once to the model cache.",
        flush=True,
    )
    return download_model(model, revision=revision, local_files_only=False)
