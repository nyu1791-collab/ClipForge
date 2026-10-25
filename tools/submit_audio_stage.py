"""Dispatch one private downstream stage, or collect and verify its finished artifact."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

from clipforge import github_cli
from clipforge.release_transfer import download_stage
from clipforge.storage import PipelineError, digest, write_json

REPO = "nyu1791-collab/ai-model-workers"
BRANCH = "fix/pr18-verified-transfer"
WORKFLOW = "clipforge-stages.yml"
# Each clip render on Colab owns its own runtime; the pool grants four or more at once.
MAX_ACTIVE = {"colab-auto": 1}
MAX_BATCH_CLIPS = 10
RESULT_STATUS = {
    "previews": "selection_ready",
    "render": "complete",
    "merge": "complete",
}
REQUIRED_FILES = {
    "audio.m4a",
    "asr-receipt.json",
    "manifest.json",
    "previews.json",
    "topics.json",
}
MAX_LISTED_FILES = 500
DEFAULT_CONFIG_FILE = "config.landscape-long-preview.example.json"
CONFIG_FILE_PATTERN = r"config\.[a-z0-9-]+\.example\.json"
SUBMIT_OPTIONS = (
    "stage",
    "previous_run",
    "previous_request",
    "source_ref",
    "review_file",
    "worker",
    "clips",
    "clip_index",
    "part_runs",
    "config_file",
)


class AudioStageError(RuntimeError):
    """A fixed, public CLI explanation; never wrap an external exception in this type."""


def command_failure(error):
    """Explain our fixed failures, keeping external details and secrets private."""
    return "Audio stage command failed: " + failure_text(error) + "\n" + RECEIPT_HINT


def gh(*args, body=None, timeout=60):
    # The shared runner never prints stderr, where gh may echo signed URLs.
    return github_cli.run(
        *args,
        body=body,
        timeout=timeout,
        message="private audio-stage GitHub operation failed; no retry",
        include_http_status=True,
    )


def save(path, receipt):
    write_json(path, receipt)


def safe_relative(name):
    """Reject paths that could leave the collected directory or name an unlisted location."""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise AudioStageError("unsafe path in stage receipt")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise AudioStageError("unsafe path in stage receipt")
    return name


def staged_files(root):
    names = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise AudioStageError("stage artifact contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise AudioStageError("stage artifact contains a non-regular entry")
        names.add(path.relative_to(root).as_posix())
    return names


def find_stage_run(identity):
    runs = json.loads(
        gh(
            "run",
            "list",
            "--repo",
            REPO,
            "--workflow",
            WORKFLOW,
            "--branch",
            BRANCH,
            "--limit",
            "100",
            "--json",
            "databaseId,displayTitle,event,headBranch,status,conclusion",
        )
    )
    matches = [
        row
        for row in runs
        if row["displayTitle"] == "ClipForge " + identity
        and row["event"] == "workflow_dispatch"
        and row["headBranch"] == BRANCH
    ]
    if len(matches) != 1:
        raise AudioStageError("expected exactly one private stage run for this request")
    run = matches[0]
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise AudioStageError(
            "private stage run did not complete successfully; no retry or fallback"
        )
    return run["databaseId"]


def previous_identity_matches(returned, request):
    """Check the previous run and request identities, including merge lineage."""
    for key in ("previous_run_id", "previous_request_id"):
        if request.get("audio_stage") != "merge" and key not in returned:
            return False
        if key in returned and str(returned[key]) != str(request[key]):
            return False
    if request.get("audio_stage") == "merge":
        parts = request.get("part_runs", "")
        returned_parts = returned.get("part_request_ids")
        if (
            returned.get("previous_request_id") != request["previous_request_id"]
            or not isinstance(parts, str)
            or not re.fullmatch(
                r"[0-9]{1,20}:[a-f0-9]{32}(,[0-9]{1,20}:[a-f0-9]{32}){0,19}",
                parts,
            )
            or not isinstance(returned_parts, list)
            or not all(isinstance(part, str) for part in returned_parts)
        ):
            return False
        return set(returned_parts) == {part.split(":")[1] for part in parts.split(",")}
    return True


def expected_status(stage, request):
    if stage == "render" and request.get("clip_index") not in (None, ""):
        return "clip_rendered"
    return RESULT_STATUS[stage]


def verify_stage_artifact(root, identity, stage, request, run_id):
    """Check the worker receipt, every listed SHA-256, extra files and generated receipts."""
    returned = json.loads((root / "request-receipt.json").read_text())
    hashes = returned.get("sha256")
    status = expected_status(stage, request)
    delivery_only = stage in {"render", "merge"}
    if (
        returned.get("request_id") != identity
        or returned.get("source_ref") != request["source_ref"]
        or returned.get("audio_stage") != stage
        or returned.get("status") != status
        or returned.get("asr_reused") is not True
        or not returned.get("audio_source_url")
        or str(returned.get("workflow_run")) != str(run_id)
        or not previous_identity_matches(returned, request)
        or not isinstance(hashes, dict)
        or not 1 <= len(hashes) <= MAX_LISTED_FILES
        or "request-receipt.json" in hashes
    ):
        raise AudioStageError("stage artifact identity mismatch")
    listed = {safe_relative(name) for name in hashes}
    for value in hashes.values():
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise AudioStageError("invalid SHA-256 entry in stage receipt")
    required = {"manifest.json"} if delivery_only else REQUIRED_FILES
    if not listed >= required:
        raise AudioStageError("stage artifact omits required files")
    found = staged_files(root) - {"request-receipt.json"}
    missing = listed - found
    extra = (
        found - listed - {".audio-first.lock"}
    )  # an empty lock file the worker may leave
    if missing:
        raise AudioStageError(
            str(len(missing)) + " listed file(s) missing from the stage artifact"
        )
    if extra:
        raise AudioStageError(
            str(len(extra)) + " unlisted file(s) in the stage artifact"
        )
    for name, value in hashes.items():
        if digest(root / name) != value:
            raise AudioStageError("stage artifact SHA-256 mismatch")
    if not delivery_only:
        cache = json.loads((root / "asr-receipt.json").read_text())
        key = cache.get("key", "")
        if (
            not cache.get("reused")
            or not isinstance(key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", key)
            or "transcripts/" + key + ".json" not in listed
        ):
            raise AudioStageError("stage ASR identity is incomplete")
    manifest = json.loads((root / "manifest.json").read_text())
    if (
        manifest.get("status") != status
        # The merge worker writes a render delivery with merge lineage in its receipt.
        or manifest.get("audio_stage")
        not in ({stage, "render"} if stage == "merge" else {stage})
        or manifest.get("source_url") != returned["audio_source_url"]
        or (
            "audio.m4a" in hashes
            and manifest.get("source_sha256") != hashes["audio.m4a"]
        )
    ):
        raise AudioStageError("stage manifest does not match the artifact receipt")
    rendered = manifest.get("rendered", [])
    if delivery_only and (not isinstance(rendered, list) or not rendered):
        raise AudioStageError("stage artifact omits required rendered clips")
    if status == "clip_rendered":
        expected = "clips/clip-%03d" % int(request["clip_index"])
        if len(rendered) != 1 or rendered[0].get("directory") not in {
            expected,
            "delivery/" + expected,
        }:
            raise AudioStageError("stage artifact clip index mismatch")
    for name in sorted(listed):
        if name.endswith(".receipt.json"):
            media = name[: -len(".receipt.json")]
            generated = json.loads((root / name).read_text())
            if (
                media not in hashes
                or generated.get("sha256") != hashes[media]
                or generated.get("source_url") != returned["audio_source_url"]
            ):
                raise AudioStageError("generated receipt does not match its media")
    for row in rendered:
        directory = safe_relative(row.get("directory"))
        if delivery_only:
            if status != "clip_rendered" and not re.fullmatch(
                r"delivery/clips/clip-[0-9]{3}", directory
            ):
                raise AudioStageError("stage manifest has an invalid clip directory")
            required = {
                directory + "/" + name
                for name in ("video.mp4", "captions.ass", "captions.srt")
            }
            if not listed >= required:
                raise AudioStageError("stage artifact omits required files")
        for name, value in row.get("sha256", {}).items():
            if hashes.get(safe_relative(directory + "/" + name)) != value:
                raise AudioStageError("rendered clip receipt does not match its file")
    return len(listed)


def config_in_source(source_ref, name):
    """The configuration file must be committed at the reviewed source revision."""
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{source_ref}:{name}"],
        cwd=repository,
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def part_config_file(root, part_runs):
    """The configuration every requested clip render used, from this machine's receipts."""
    names = set()
    for item in part_runs.split(","):
        path = root / item.split(":")[1] / "receipt.json"
        if path.is_file():
            names.add(
                json.loads(path.read_text()).get("config_file", DEFAULT_CONFIG_FILE)
            )
    if not all(
        isinstance(name, str) and re.fullmatch(CONFIG_FILE_PATTERN, name)
        for name in names
    ):
        raise AudioStageError("invalid clip render configuration receipt")
    if len(names) > 1:
        raise AudioStageError("clip renders use different configuration files")
    return names.pop() if names else DEFAULT_CONFIG_FILE


def prepare_destination(destination):
    destination = Path(destination).absolute()
    if destination.exists() or any(
        part.is_symlink() for part in [destination, *destination.parents]
    ):
        raise AudioStageError(
            "download destination already exists or uses a symlink; choose a new directory"
        )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return destination


def local_merge(part_runs, destination, source_ref=None):
    """Download verified clip runs and join locally, without dispatching a workflow."""
    from clipforge import worker_audio
    from clipforge.models import Settings

    if not re.fullmatch(
        r"[0-9]{1,20}:[a-f0-9]{32}(,[0-9]{1,20}:[a-f0-9]{32}){0,19}", part_runs
    ) or (source_ref is not None and not re.fullmatch(r"[a-f0-9]{40}", source_ref)):
        raise AudioStageError("invalid local merge identity")
    destination = prepare_destination(destination)
    with tempfile.TemporaryDirectory(
        prefix=".clipforge-merge-", dir=destination.parent
    ) as temporary:
        root = Path(temporary)
        parts = worker_audio.download_parts(part_runs, root / "parts")
        configs = {worker_audio.receipt_config_file(path) for path, _ in parts}
        if len(configs) != 1:
            raise AudioStageError("clip renders use different configuration files")
        config_file = configs.pop()
        receipt = json.loads(
            worker_audio.private_member(parts[0][0], "request-receipt.json").read_text()
        )
        source_ref = source_ref or receipt.get("source_ref")
        if not isinstance(source_ref, str) or not re.fullmatch(
            r"[a-f0-9]{40}", source_ref
        ):
            raise AudioStageError("invalid local merge source identity")
        first = worker_audio.load_part(parts[0][0], parts[0][1], source_ref)
        request = {
            "audio_stage": "merge",
            "source_ref": source_ref,
            "part_runs": part_runs,
            "previous_request_id": first["identity"]["previous_request_id"],
            "config_file": config_file,
        }
        if not re.fullmatch(r"[a-f0-9]{32}", str(request["previous_request_id"])):
            raise AudioStageError("invalid local merge analysis identity")
        identity = hashlib.sha256(
            json.dumps(request, sort_keys=True).encode()
        ).hexdigest()[:32]
        repository = Path(__file__).resolve().parents[1]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        config = (
            (repository / config_file).read_text()
            if head == source_ref
            else subprocess.run(
                ["git", "show", source_ref + ":" + config_file],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        )
        settings = Settings.model_validate_json(config)
        # A source config alone cannot make different executing code reproducible.
        if (
            head != source_ref
            or worker_audio.landscape.code_identity().get("git_head") != source_ref
        ):
            raise AudioStageError("local merge code differs from source_ref")
        # Parts retain no reviewed selection or previous request settings. Check the
        # source configuration's plan; never derive the expected count from the parts.
        staging = root / "result"
        result, lineage = worker_audio.merge_parts(
            parts, staging, settings, source_ref, config_file=config_file
        )
        worker_audio.write_receipt(
            staging,
            result,
            lineage,
            identity,
            source_ref,
            "merge",
            "local",
            config_file=config_file,
        )
        count = verify_stage_artifact(staging, identity, "merge", request, "local")
        if destination.exists() or destination.is_symlink():
            raise AudioStageError("merge destination was created by another operation")
        shutil.move(str(staging), str(destination))
    print(
        json.dumps(
            {
                "request_id": identity,
                "stage": "merge",
                "status": "COLLECTED",
                "verified_files": count,
                "output": str(destination),
                "local": True,
            }
        )
    )
    return 0


def collect(identity, destination):
    """Recover one finished private stage into a new directory; nothing is dispatched."""
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise AudioStageError("invalid request identifier")
    state = Path.home() / ".local/state/clipforge" / identity
    request = json.loads((state / "request.json").read_text())
    receipt = json.loads((state / "receipt.json").read_text())
    stage = request.get("audio_stage")
    if (
        stage not in RESULT_STATUS
        or receipt.get("repository") != REPO
        or receipt.get("worker_ref") != BRANCH
        or receipt.get("request_id") != identity
        or request.get("request_id") != identity
        or request.get("input_tag") != "clipforge-input-" + identity
        or receipt.get("audio_stage") != stage
        or receipt.get("source_ref") != request.get("source_ref")
        or receipt.get("previous_run_id") != request.get("previous_run_id")
        or receipt.get("config_file") != request.get("config_file")
        or not re.fullmatch(r"[a-f0-9]{40}", str(request.get("source_ref")))
        or not re.fullmatch(r"[0-9]{1,20}", str(request.get("previous_run_id")))
        or not re.fullmatch(r"[a-f0-9]{32}", str(request.get("previous_request_id")))
    ):
        raise AudioStageError("saved stage request identity mismatch")
    destination = prepare_destination(destination)
    run_id = find_stage_run(identity)
    with tempfile.TemporaryDirectory(
        prefix=".clipforge-collect-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "result"
        download_stage(run_id, "clipforge-result-" + identity, staging, gh)
        count = verify_stage_artifact(staging, identity, stage, request, run_id)
        if destination.exists() or destination.is_symlink():
            raise AudioStageError(
                "download destination was created by another operation"
            )
        shutil.move(str(staging), str(destination))
    receipt.update(run_id=run_id, status="COLLECTED", output=str(destination))
    save(state / "receipt.json", receipt)
    print(
        json.dumps(
            {
                "request_id": identity,
                "stage": stage,
                "status": "COLLECTED",
                "verified_files": count,
                "output": str(destination),
            }
        )
    )
    return 0


CROP_ANCHORS = ("auto", "left", "center", "right")


def require_video_review(value):
    """Require a usable selection JSON without a separate video-approval gate."""
    clips = value.get("clips") if isinstance(value, dict) else None
    if (
        not isinstance(clips, list)
        or not clips
        or any(not isinstance(row, dict) for row in clips)
    ):
        raise AudioStageError("selected topics must be JSON objects")
    # Same set as pipeline.CROP_ANCHORS; this tool does not import the package.
    if any(row.get("crop_anchor", "auto") not in CROP_ANCHORS for row in clips):
        raise AudioStageError("crop_anchor must be auto, left, center or right")
    # Same range as pipeline.MAX_CAPTION_FLOOR; a missing floor means none is set.
    for row in clips:
        floor = row.get("caption_floor")
        if floor is not None and (
            type(floor) not in (int, float) or not 0 <= floor <= 0.32
        ):
            raise AudioStageError("caption_floor must be from 0 to 0.32")
    for row in clips:
        edits = row.get("caption_edits", [])
        if not isinstance(edits, list) or any(
            not isinstance(e, dict)
            or set(e) != {"at", "from", "to"}
            or type(e["at"]) not in (int, float)
            or not 0 <= e["at"] <= 1800
            or not isinstance(e["from"], str)
            or not 0 < len(e["from"]) <= 100
            or not isinstance(e["to"], str)
            or len(e["to"]) > 100
            for e in edits
        ):
            raise AudioStageError("caption_edits must list at, from and to")


def clip_indices(args, worker):
    """One GPU owns the reviewed batch; clip-index remains for a targeted repair."""
    if args.clips is not None and args.clip_index is not None:
        raise AudioStageError("use either --clips or --clip-index")
    if args.stage != "render" and (args.clips or args.clip_index):
        raise AudioStageError("clip numbers apply to rendering only")
    if args.clip_index is not None:
        if not 1 <= args.clip_index <= 20:
            raise AudioStageError("clip index must be 1 to 20")
        return [args.clip_index]
    if args.clips is not None:
        if not 5 <= args.clips <= MAX_BATCH_CLIPS or worker != "colab-auto":
            raise AudioStageError(
                "batch rendering needs --worker colab-auto and 5 to 10 clips"
            )
        return [None]
    return [None]


def dispatch(root, inputs, args):
    identity = inputs["request_id"]
    state = root / identity
    state.mkdir(mode=0o700)
    (state / "request.json").write_text(
        json.dumps(inputs, ensure_ascii=False, indent=2)
    )
    receipt = {
        "request_id": identity,
        "repository": REPO,
        "source_ref": args.source_ref,
        "worker_ref": BRANCH,
        "status": "DISPATCHING",
        "audio_stage": args.stage,
        "previous_run_id": args.previous_run,
        "worker": inputs["worker"],
        "config_file": inputs["config_file"],
        "clip_index": inputs["clip_index"] or None,
        "job_submitted": None,
    }
    path = state / "receipt.json"
    path.write_text(json.dumps(receipt, indent=2))
    try:
        gh(
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
            "--input",
            "-",
            body=json.dumps({"ref": BRANCH, "inputs": inputs}),
        )
    except Exception:
        receipt["status"] = "DISPATCH_UNCONFIRMED"
        path.write_text(json.dumps(receipt, indent=2))
        raise
    receipt.update(status="DISPATCHED", job_submitted=True)
    path.write_text(json.dumps(receipt, indent=2))
    print(
        json.dumps(
            {
                "request_id": identity,
                "stage": args.stage,
                "clip_index": receipt["clip_index"],
                "status": "DISPATCHED",
            }
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("previews", "render", "merge"))
    parser.add_argument(
        "--worker",
        choices=("cpu", "colab-auto"),
        help="render on the hosted runner (default) or on Colab runtimes",
    )
    parser.add_argument(
        "--clips", type=int, help="render N approved clips in one GPU batch"
    )
    parser.add_argument("--clip-index", type=int, help="render only this clip")
    parser.add_argument(
        "--config-file",
        help="render only: committed config.NAME.example.json at --source-ref "
        "(default " + DEFAULT_CONFIG_FILE + ")",
    )
    parser.add_argument(
        "--part-runs", help="merge: comma-separated RUN_ID:REQUEST_ID of clip renders"
    )
    parser.add_argument("--previous-run")
    parser.add_argument("--previous-request")
    parser.add_argument("--source-ref")
    parser.add_argument("--review-file", type=Path)
    parser.add_argument(
        "--collect",
        metavar="REQUEST_ID",
        help="verify and download one finished stage artifact into a new --output",
    )
    parser.add_argument("--local", action="store_true", help="merge on this machine")
    parser.add_argument(
        "--output", type=Path, help="new directory for --collect or --local"
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.collect is not None:
        if (
            args.local
            or args.output is None
            or any(getattr(args, name) is not None for name in SUBMIT_OPTIONS)
        ):
            parser.error("--collect requires --output and no submission options")
        return collect(args.collect, args.output)
    if args.local:
        if (
            args.stage != "merge"
            or not args.part_runs
            or args.output is None
            or any(
                getattr(args, name) is not None
                for name in SUBMIT_OPTIONS
                if name not in {"stage", "part_runs", "source_ref"}
            )
        ):
            parser.error("--local requires --stage merge, --part-runs and --output")
        return local_merge(args.part_runs, args.output, args.source_ref)
    if args.output is not None:
        parser.error("--output is only used with --collect or --local")
    missing = [
        "--" + name.replace("_", "-")
        for name in ("stage", "previous_run", "previous_request", "source_ref")
        if getattr(args, name) is None
    ]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))
    if (
        not re.fullmatch(r"[0-9]{1,20}", args.previous_run)
        or not re.fullmatch(r"[a-f0-9]{32}", args.previous_request)
        or not re.fullmatch(r"[a-f0-9]{40}", args.source_ref)
    ):
        raise AudioStageError("invalid immutable source or analysis identity")
    review = json.loads(args.review_file.read_text()) if args.review_file else None
    if args.stage == "render":
        if review is None:
            raise AudioStageError("render requires reviewed topic intervals")
        require_video_review(review)
        if args.clips is not None and args.clips != len(review["clips"]):
            raise AudioStageError("--clips must match the number of approved clips")
    worker = args.worker or ("colab-auto" if args.stage == "render" else "cpu")
    if args.stage == "render" and worker != "colab-auto":
        raise AudioStageError("render requires the Parakeet GPU/NVENC production route")
    indices = clip_indices(args, worker)
    part_runs = args.part_runs or ""
    if (args.stage == "merge") != bool(part_runs) or (
        part_runs
        and not re.fullmatch(
            r"[0-9]{1,20}:[a-f0-9]{32}(,[0-9]{1,20}:[a-f0-9]{32}){0,19}", part_runs
        )
    ):
        raise AudioStageError(
            "merge needs --part-runs RUN_ID:REQUEST_ID,... and only merge"
        )
    if args.stage != "render" and worker != "cpu":
        raise AudioStageError("only clip rendering runs on Colab")
    if args.config_file is not None and args.stage != "render":
        raise AudioStageError("--config-file applies to render only")
    if args.stage == "merge":
        config_file = part_config_file(
            Path.home() / ".local/state/clipforge", part_runs
        )
    else:
        config_file = args.config_file or DEFAULT_CONFIG_FILE
    if args.stage == "render":
        if not re.fullmatch(CONFIG_FILE_PATTERN, config_file):
            raise AudioStageError("configuration file name is not allowed")
        if not config_in_source(args.source_ref, config_file):
            raise AudioStageError(
                "configuration file is not committed at --source-ref; nothing was dispatched"
            )
    base = {
        "source_ref": args.source_ref,
        "audio_stage": args.stage,
        "previous_run_id": args.previous_run,
        "previous_request_id": args.previous_request,
        "review_json": json.dumps(review, ensure_ascii=False)
        if review is not None
        else "",
        "worker": worker,
        "part_runs": part_runs,
        "config_file": config_file,
    }
    if len(base["review_json"].encode()) > 60000:
        raise AudioStageError("review metadata is oversized")
    requests = []
    for index in indices:
        inputs = {**base, "clip_index": str(index) if index else ""}
        identity = hashlib.sha256(
            json.dumps(inputs, sort_keys=True).encode()
        ).hexdigest()[:32]
        inputs.update(request_id=identity, input_tag="clipforge-input-" + identity)
        requests.append(inputs)
    root = Path.home() / ".local/state/clipforge"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".submit.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AudioStageError("another ClipForge submission is active") from None
        for inputs in requests:
            if (root / inputs["request_id"]).exists():
                raise AudioStageError(
                    "this exact stage already has a request; inspect it before retrying"
                )
        if gh("api", "user", "--jq", ".login").strip() != "nyu1791-collab":
            raise AudioStageError("the approved owner connection is required")
        if not gh("api", "repos/" + REPO, "--jq", ".private").strip() == "true":
            raise AudioStageError("audio stages require the private worker repository")
        runs = json.loads(
            gh(
                "run",
                "list",
                "--repo",
                REPO,
                "--workflow",
                WORKFLOW,
                "--branch",
                BRANCH,
                "--limit",
                "100",
                "--json",
                "status",
            )
        )
        active = sum(row["status"] != "completed" for row in runs)
        if active + len(requests) > MAX_ACTIVE.get(worker, 1):
            raise AudioStageError(
                "another worker is active; no duplicate stage was submitted"
            )
        for inputs in requests:
            dispatch(root, inputs, args)
    return 0


RECEIPT_HINT = (
    "依頼が作られた後の失敗なら ~/.local/state/clipforge/<依頼ID>/receipt.json を確認する。"
    "自動の再送や代替は行っていない。"
)


def failure_text(error):
    """Only explicitly public errors carry detail; process and parser payloads stay private."""
    name = type(error).__name__
    if isinstance(error, subprocess.CalledProcessError):
        return name + ": exit " + str(error.returncode)
    if isinstance(error, subprocess.TimeoutExpired):
        return name + ": timed out"
    if isinstance(error, OSError):
        return (
            name + ": " + os.strerror(error.errno)
            if isinstance(error.errno, int)
            else name
        )
    if isinstance(error, AudioStageError):
        text = " ".join(str(error).split())[:300]
        return name + ": " + text if text else name
    return name


def run_cli(argv=None):
    try:
        return main(argv)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        PipelineError,
        subprocess.SubprocessError,
    ) as error:
        print(command_failure(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
