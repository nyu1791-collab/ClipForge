"""Version saved editorial data without sending audio or subtitles to a model."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import uuid

from .caption_emotions import annotation
from .models import Transcript
from .speaker_captions import validate_turns
from .sound_effects import prepare_cues
from .storage import PipelineError, digest, fingerprint, write_json

PRIVATE_REPO = "nyu1791-collab/ai-model-workers"
EDITORIAL_TAG_PATTERN = (
    r"clipforge-kick-editorial-(?:(?:replay-)?[0-9]+|revision-[a-f0-9]{32})"
)
ASSETS = (
    "audio-editor-receipt.json",
    "final-selection.json",
    "selected-transcripts.json",
)


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise PipelineError(
            "Private editorial revision operation failed; no resubmission"
        )
    return result.stdout


def create_revision(parent_tag, patch_file, output, *, publish=False):
    """Apply explicit edits to a new private directory; originals and model receipts remain."""
    if not re.fullmatch(EDITORIAL_TAG_PATTERN, parent_tag):
        raise PipelineError("Invalid parent editorial release")
    patch_file, output = Path(patch_file), Path(output)
    patch = json.loads(patch_file.read_text())
    # A mistake in target IDs is an input error, not an editorial quality gate.
    if set(patch) - {"reason", "clips", "input_fingerprint"} or not isinstance(patch.get("clips"), list):
        raise PipelineError(
            "Revision requires reason and clips; source replacement is not supported"
        )
    if output.exists():
        raise PipelineError(
            "Revision output already exists; use its saved state instead of resubmitting"
        )
    private = json.loads(gh("api", f"repos/{PRIVATE_REPO}"))
    if private.get("private") is not True:
        raise PipelineError("Editorial storage must be private")
    output.mkdir(parents=True, mode=0o700)
    parent = output / "parent"
    parent.mkdir(mode=0o700)
    release = json.loads(gh("api", f"repos/{PRIVATE_REPO}/releases/tags/{parent_tag}"))
    assets = release.get("assets", [])
    for name in ASSETS:
        matches = [asset for asset in assets if asset.get("name") == name]
        if (
            len(matches) != 1
            or type(matches[0].get("id")) is not int
            or type(matches[0].get("size")) is not int
            or not 0 < matches[0]["size"] <= 8 * 1024**2
        ):
            raise PipelineError("Private editorial asset is missing or invalid")
        target = parent / name
        with target.open("xb") as destination:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{PRIVATE_REPO}/releases/assets/{matches[0]['id']}",
                    "-H",
                    "Accept: application/octet-stream",
                ],
                stdout=destination,
                stderr=subprocess.PIPE,
                timeout=600,
            )
        if result.returncode or target.stat().st_size != matches[0]["size"]:
            target.unlink(missing_ok=True)
            raise PipelineError("Private editorial asset download failed")
    receipt = json.loads((parent / ASSETS[0]).read_text())
    selection = json.loads((parent / ASSETS[1]).read_text())
    transcripts = json.loads((parent / ASSETS[2]).read_text())
    if "input_fingerprint" in patch and patch["input_fingerprint"] != fingerprint({"selection": selection, "transcripts": transcripts}):
        raise PipelineError("Revision was prepared for another editorial selection")
    if (
        receipt.get("status") != "complete"
        or selection.get("selection_basis") != "audio_understanding"
    ):
        raise PipelineError(
            "A completed audio-understanding editorial bundle is required"
        )
    if len(selection["clips"]) != len(transcripts["clips"]):
        raise PipelineError("Saved editorial clip mapping is missing")
    changed = set()
    for row in patch["clips"]:
        if not isinstance(row, dict) or set(row) - {
            "clip",
            "title",
            "segments",
            "add_segments",
            "sound_effects",
            "speaker_turns",
        }:
            raise PipelineError("Unsupported revision fields")
        index = row.get("clip")
        if type(index) is not int or not 1 <= index <= len(selection["clips"]):
            raise PipelineError("Revision clip does not exist")
        clip = selection["clips"][index - 1]
        data = transcripts["clips"][index - 1]
        before = deepcopy((clip, data))
        if "title" in row:
            if not isinstance(row["title"], str):
                raise PipelineError("Revision title must be text")
            clip["title"] = row["title"].strip() or "話題"
        if not isinstance(row.get("segments", []), list):
            raise PipelineError("Revision segments must be a list")
        for change in row.get("segments", []):
            if not isinstance(change, dict) or set(change) - {
                "segment",
                "text",
                "start",
                "end",
                "emotion",
                "emotion_level",
                "emotion_confidence",
            }:
                raise PipelineError("Unsupported subtitle revision fields")
            sid = change.get("segment")
            if type(sid) is not int or not 1 <= sid <= len(data["segments"]):
                raise PipelineError("Revision subtitle does not exist")
            segment = data["segments"][sid - 1]
            if any(
                field in change and change[field] != segment.get(field)
                for field in ("text", "start", "end")
            ):
                # Old word tokens would otherwise override the explicitly edited phrase.
                segment["words"] = []
            segment.update({k: v for k, v in change.items() if k != "segment"})
            if any(k.startswith("emotion") for k in change):
                segment.update(annotation(segment))
        additions = row.get("add_segments", [])
        if not isinstance(additions, list):
            raise PipelineError("Added subtitles must be a list")
        for added in additions:
            if not isinstance(added, dict) or set(added) != {"start", "end", "text"}:
                raise PipelineError("Added subtitle requires start, end and text")
            start, end, text = added["start"], added["end"], added["text"]
            if (type(start) not in (int, float) or type(end) not in (int, float)
                or not isinstance(text, str) or not text.strip()
                or not 0 <= start < end <= clip["end"] - clip["start"]):
                raise PipelineError("Added subtitle has invalid time or text")
            data["segments"].append({"start": start, "end": end, "text": text, "words": []})
        if additions:
            data["segments"].sort(key=lambda item: (item["start"], item["end"]))
        # Preserve clip identity and real data structure; no text length or quality threshold.
        transcripts["clips"][index - 1] = Transcript.model_validate(data).model_dump()
        if "speaker_turns" in row:
            clip["speaker_turns"] = validate_turns(
                row["speaker_turns"], clip["end"] - clip["start"]
            )
        if additions:
            clip["speaker_turns"] = validate_turns(
                [*clip.get("speaker_turns", []), *[{"start": item["start"], "end": item["end"], "speaker_id": None, "confidence": 0} for item in additions]],
                clip["end"] - clip["start"],
            )
        if "sound_effects" in row:
            clip["sound_effects"] = prepare_cues(
                row["sound_effects"], clip["end"] - clip["start"]
            )
        if before != (clip, transcripts["clips"][index - 1]):
            changed.add(index)
    tag = "clipforge-kick-editorial-revision-" + uuid.uuid4().hex
    revision = {
        "release": tag,
        "parent_release": parent_tag,
        "parent_sha256": {name: digest(parent / name) for name in ASSETS},
        "patch_sha256": digest(patch_file),
        "changed_clips": sorted(changed),
        "reason": str(patch.get("reason") or "Explicit editorial correction"),
        "model_requests": 0,
    }
    selection["editorial_revision"] = revision
    # Keep the original generator/model/rulebook records; label manual changes separately.
    receipt["editorial_revision"] = revision
    write_json(output / ASSETS[0], receipt)
    write_json(output / ASSETS[1], selection)
    write_json(output / ASSETS[2], transcripts)
    write_json(output / "revision-patch.json", patch)
    state = {**revision, "status": "prepared", "source_url": selection["source_url"]}
    write_json(output / "revision-state.json", state)
    return publish_revision(output) if publish else state


def publish_revision(output):
    """Publish a prepared version once. Unknown publication state is never retried here."""
    output = Path(output)
    state_path = output / "revision-state.json"
    state = json.loads(state_path.read_text())
    if state.get("status") != "prepared":
        raise PipelineError(
            "Revision publication already attempted; inspect its release"
        )
    state["status"] = "publishing_or_unknown"
    write_json(state_path, state)
    gh(
        "release",
        "create",
        state["release"],
        *(str(output / name) for name in (*ASSETS, "revision-patch.json")),
        "--repo",
        PRIVATE_REPO,
        "--latest=false",
        "--notes",
        "Private editorial revision; original model responses preserved; no model requests.",
    )
    state["status"] = "published"
    write_json(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-release", required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            create_revision(
                args.parent_release, args.patch, args.output, publish=args.publish
            )
        )
    )


if __name__ == "__main__":
    main()
