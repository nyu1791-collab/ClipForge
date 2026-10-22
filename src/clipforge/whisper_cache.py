"""Resolve the exact requested model from local files before contacting the Hub."""

import os
from pathlib import Path

from .storage import PipelineError


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
