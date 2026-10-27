"""Read-only collection of completed clips without another production request."""

import hashlib
import json
from pathlib import Path
import re
import os
import shutil
import tempfile

from .github_cli import run as gh
from .media_sources import canonical
from .release_transfer import download, inventory, read_transfer, sha, validate_identity
from .storage import write_json


def collect_clip(output, index):
    output = Path(output)
    state = json.loads((output / "production-state.json").read_text())
    stages = [s for s in state["stages"] if s.get("workflow") == "clipforge-stages.yml"]
    if (
        len(stages) != 1
        or type(index) is not int
        or not 1 <= index <= state["target_clips"]
    ):
        raise RuntimeError("Saved production or clip number is invalid")
    stage = stages[0]
    parent, run_id = stage.get("request_id", ""), str(stage.get("run_id") or "")
    source_ref = state.get("source_ref", "")
    if (
        not re.fullmatch(r"[a-f0-9]{32}", parent)
        or not re.fullmatch(r"[1-9][0-9]*", run_id)
        or not re.fullmatch(r"[a-f0-9]{40}", source_ref)
    ):
        raise RuntimeError("Saved production identity is incomplete")
    identity = hashlib.sha256((parent + ":render:" + str(index)).encode()).hexdigest()[
        :32
    ]
    name = "clipforge-result-" + identity
    metadata = read_transfer(run_id, name)
    receipt, manifest = (
        metadata.get("request_receipt", {}),
        metadata.get("stage_manifest", {}),
    )
    expected = "clips/clip-%03d" % index
    rows = manifest.get("rendered", [])
    if (
        receipt.get("request_id") != identity
        or receipt.get("source_ref") != source_ref
        or receipt.get("status") != "clip_rendered"
        or manifest.get("status") != "clip_rendered"
        or canonical(manifest.get("source_url", "")) != canonical(state["source_url"])
        or len(rows) != 1
        or rows[0].get("directory") != expected
    ):
        raise RuntimeError("Saved clip does not match the requested production")
    destination = output / "completed" / ("clip-%03d" % index)
    if (output / "completed").is_symlink() or destination.is_symlink():
        raise RuntimeError("Collected clip directory must not be a symlink")
    if destination.exists():
        validate_identity(destination, run_id, name, source_ref, audit_hashes=False)
        local = json.loads((destination / "manifest.json").read_text())
        if local != manifest or not (destination / expected / "video.mp4").is_file():
            raise RuntimeError("Existing local clip does not match the saved result")
        status = "already_collected"
    else:
        download(run_id, name, destination, expected=metadata)
        status = "collected"
    return {
        "status": status,
        "clip": index,
        "video": str(destination / expected / "video.mp4"),
    }


def assemble_collected(output, run_id, request_id, source_ref, metadata):
    """Build the final local result from individual verified transfers, without redownloading them."""
    output = Path(output)
    root = output / "clips"
    saved_manifest = metadata.get("stage_manifest", {})
    receipt = metadata.get("request_receipt", {})
    # transfer.json is sorted recursively. Recreate the producer's top-level
    # manifest order so its recorded SHA remains meaningful during assembly.
    manifest_keys = (
        "status",
        "rendered",
        "reused_clips",
        "newly_rendered_clips",
        "editorial_revision",
        "source_url",
        "full_source_asr",
        "timestamp_source",
        "full_video_quality_audit",
        "max_gpu_slots",
    )
    if set(saved_manifest) != set(manifest_keys):
        raise RuntimeError("Final manifest format cannot be assembled locally")
    manifest = {key: saved_manifest[key] for key in manifest_keys}
    rows = manifest.get("rendered", [])
    if (
        manifest.get("status") != "complete"
        or receipt.get("status") != "complete"
        or receipt.get("request_id") != request_id
        or receipt.get("source_ref") != source_ref
        or not isinstance(rows, list)
        or not rows
    ):
        raise RuntimeError("Final render metadata is incomplete")
    with tempfile.TemporaryDirectory(dir=output, prefix=".collect-") as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir(mode=0o700)
        (bundle / "clips").mkdir(mode=0o700)
        seen = set()
        for row in rows:
            match = re.fullmatch(
                r"clips/clip-([0-9]{3})", str(row.get("directory", ""))
            )
            if not match or int(match[1]) in seen:
                raise RuntimeError("Final clip identity is invalid")
            index = int(match[1])
            seen.add(index)
            collect_clip(output, index)
            part = output / "completed" / ("clip-%03d" % index)
            single = json.loads((part / "manifest.json").read_text())
            if single.get("rendered") != [row]:
                raise RuntimeError("Collected clip differs from the final result")
            source = part / "clips" / ("clip-%03d" % index)
            target = bundle / "clips" / ("clip-%03d" % index)

            def link_or_copy(src, dst):
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                return dst

            shutil.copytree(source, target, copy_function=link_or_copy)
        editorial = bundle / "audio-review"
        editorial.mkdir(mode=0o700)
        gh(
            "release",
            "download",
            "clipforge-kick-editorial-" + str(run_id),
            "--repo",
            "nyu1791-collab/ai-model-workers",
            "--pattern",
            "final-selection.json",
            "--pattern",
            "selected-transcripts.json",
            "--pattern",
            "audio-editor-receipt.json",
            "--dir",
            str(editorial),
            timeout=600,
        )
        write_json(bundle / "manifest.json", manifest)
        write_json(bundle / "request-receipt.json", receipt)
        if metadata.get("gpu_receipt") is not None:
            write_json(bundle / "gpu-receipt.json", metadata["gpu_receipt"])
        if inventory(bundle, include_hashes=False) != metadata.get("files"):
            raise RuntimeError(
                "Collected files do not match the final transfer inventory"
            )
        if sha(bundle / "manifest.json") != receipt.get("sha256", {}).get(
            "manifest.json"
        ):
            raise RuntimeError("Collected final manifest differs from the producer")
        validate_identity(
            bundle,
            run_id,
            "clipforge-result-" + request_id,
            source_ref,
            audit_hashes=False,
        )
        if root.exists() or root.is_symlink():
            raise RuntimeError("Final output already exists")
        bundle.rename(root)
