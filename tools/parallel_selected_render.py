#!/usr/bin/env python3
"""Render saved audio-understanding clips with at most SLOT_COUNT owned Colab runtimes."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clipforge.release_transfer import (
    download,
    private_repo,
    publish,
    read_transfer,
    release,
    sha,
)
from clipforge.storage import write_json
from clipforge.gpu_slots import SLOT_COUNT, clear_stopped


def command(args, env):
    result = subprocess.run(args, env=env, check=False)
    if result.returncode:
        raise RuntimeError(
            "Parallel render worker failed; inspect its saved private request"
        )


def main(collect_only=False, partial=False):
    os.umask(0o077)
    start = time.monotonic()
    review = Path(os.environ["REVIEW_FILE"])
    selection = json.loads(review.read_text())
    if selection.get("selection_basis") != "audio_understanding":
        raise RuntimeError(
            "Parallel rendering requires a saved audio-understanding selection"
        )
    count = len(selection["clips"])
    if not 1 <= count <= 10:
        raise RuntimeError("Invalid saved clip count")
    run_id = os.environ["GITHUB_RUN_ID"]
    parent = os.environ["REQUEST_ID"]
    source_ref = os.environ["CLIPFORGE_SOURCE_REF"]
    root = Path(os.environ["RUNNER_TEMP"]) / "parallel-selected-render"
    root.mkdir(mode=0o700, exist_ok=True)
    private_repo()
    controller = Path(__file__).with_name("colab_actions.py")
    worker_ids = [
        hashlib.sha256((parent + ":render:" + str(i)).encode()).hexdigest()[:32]
        for i in range(1, count + 1)
    ]
    groups = [
        list(range(slot + 1, count + 1, min(SLOT_COUNT, count))) for slot in range(min(SLOT_COUNT, count))
    ]

    def worker(slot):
        indices = groups[slot]
        identity = hashlib.sha256(
            (parent + ":render-shard:" + str(slot)).encode()
        ).hexdigest()[:32]
        tag = "clipforge-input-" + identity
        result_name = "clipforge-result-" + identity
        existing = release(result_name + "-run-" + run_id)
        if existing is not None:
            record = read_transfer(run_id, result_name)
            if record.get("request_receipt", {}).get("status") != "complete":
                raise RuntimeError("Saved render shard is not complete")
            return
        if release(tag) is not None:
            raise RuntimeError(
                "Render request was already attempted; inspect its private state"
            )
        env = {
            **os.environ,
            "REQUEST_ID": identity,
            "INPUT_TAG": tag,
            "CLIP_INDEX": "",
            "CLIPFORGE_RENDER_SHARD": ",".join(str(index) for index in indices),
            "CLIPFORGE_PARENT_REQUEST": parent,
            "CLIPFORGE_SHARED_BALANCE": "1",
            "CLIPFORGE_ALLOCATION_WAIT_SECONDS": "600",
        }
        command(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                "nyu1791-collab/ai-model-workers",
                "--latest=false",
                "--notes",
                "Private parallel render request marker.",
            ],
            env,
        )
        state = root / ("control-" + str(slot))
        output = root / ("receipt-" + str(slot))
        try:
            command(
                [
                    sys.executable,
                    str(controller),
                    "start",
                    "--mode",
                    "audio-stage",
                    "--state-dir",
                    str(state),
                ],
                env,
            )
            command(
                [
                    sys.executable,
                    str(controller),
                    "run",
                    "--state-dir",
                    str(state),
                    "--output",
                    str(output),
                ],
                env,
            )
        finally:
            if (state / "state.json").exists():
                command(
                    [
                        sys.executable,
                        str(controller),
                        "stop",
                        "--state-dir",
                        str(state),
                    ],
                    env,
                )

    errors, reported = [], set()
    if not collect_only:
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            pending = {pool.submit(worker, slot) for slot in range(len(groups))}
            while pending:
                done, pending = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        future.result()
                    except Exception as error:
                        errors.append(error)
                for index, identity in enumerate(worker_ids, 1):
                    if index in reported:
                        continue
                    try:
                        record = read_transfer(run_id, "clipforge-result-" + identity)
                    except Exception:
                        continue
                    if (
                        record.get("request_receipt", {}).get("status")
                        == "clip_rendered"
                    ):
                        reported.add(index)
                        gpu_record = record.get("gpu_receipt") or {}
                        started_at = gpu_record.get("started_at")
                        elapsed = (
                            max(0, time.time() - started_at)
                            if isinstance(started_at, (int, float))
                            else gpu_record.get("elapsed_seconds")
                        )
                        elapsed_text = (
                            "%.1f秒" % elapsed
                            if isinstance(elapsed, (int, float))
                            else "不明"
                        )
                        print(
                            "[%d/%d 完了] clip-%03d (保存確認まで: %s)"
                            % (len(reported), count, index, elapsed_text),
                            flush=True,
                        )
    merged = root / ("merged-partial" if partial else "merged")
    merged.mkdir(mode=0o700)
    (merged / "clips").mkdir(mode=0o700)
    rows = []
    gpu_parts = []
    full_review = Path(os.environ["RUNNER_TEMP"]) / "editorial-render"
    final_transcripts = (
        json.loads((full_review / "selected-transcripts.json").read_text())
        if collect_only
        else None
    )
    for index, identity in enumerate(worker_ids, 1):
        if (
            partial
            and release("clipforge-result-" + identity + "-run-" + run_id) is None
        ):
            continue
        if not partial and errors and index not in reported:
            continue
        part = root / ("part-" + str(index))
        transfer = download(run_id, "clipforge-result-" + identity, part)
        manifest = transfer["stage_manifest"]
        rendered = manifest.get("rendered", [])
        expected = "clips/clip-%03d" % index
        if (
            manifest.get("status") != "clip_rendered"
            or manifest.get("source_url") != selection["source_url"]
            or len(rendered) != 1
            or rendered[0].get("directory") != expected
        ):
            raise RuntimeError(
                "Parallel render result does not match the selected clip"
            )
        if collect_only:
            part_review = json.loads(
                (part / "audio-review/final-selection.json").read_text()
            )
            part_transcripts = json.loads(
                (part / "audio-review/selected-transcripts.json").read_text()
            )
            if (
                part_review.get("clips", [])[index - 1] != selection["clips"][index - 1]
                or part_transcripts.get("clips", [])[index - 1]
                != final_transcripts.get("clips", [])[index - 1]
            ):
                raise RuntimeError(
                    "Saved clip editorial data differs from the final selection"
                )
        shutil.move(str(part / expected), str(merged / expected))
        rows.extend(rendered)
        gpu_parts.append(
            {
                "clip": index,
                "request_id": identity,
                "receipt": transfer.get("gpu_receipt"),
            }
        )
        if not collect_only and not (merged / "audio-review").exists():
            shutil.copytree(part / "audio-review", merged / "audio-review")
    if collect_only:
        shutil.copytree(full_review, merged / "audio-review")
    manifest = {
        "status": "partial" if errors or partial else "complete",
        "rendered": rows,
        "reused_clips": sum(row.get("reused") is True for row in rows),
        "newly_rendered_clips": sum(row.get("reused") is not True for row in rows),
        "editorial_revision": selection.get("editorial_revision"),
        "source_url": selection["source_url"],
        "full_source_asr": False,
        "timestamp_source": (json.loads((review.parent / "selected-transcripts.json").read_text()).get("timestamp_source", "unknown") if (review.parent / "selected-transcripts.json").is_file() else "unknown"),
        "full_video_quality_audit": "not_run",
        "max_gpu_slots": min(SLOT_COUNT, count),
    }
    write_json(merged / "manifest.json", manifest)
    write_json(
        merged / "gpu-receipt.json",
        {
            "mode": "parallel-audio-stage",
            "status": "partial" if errors or partial else "complete",
            "human_review_performed": False,
            "parts": gpu_parts,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "elapsed_scope": "aggregation_only"
            if collect_only
            else "render_and_aggregation",
        },
    )
    write_json(
        merged / "request-receipt.json",
        {
            "request_id": parent,
            "source_ref": source_ref,
            "workflow_run": run_id,
            "status": "partial" if errors or partial else "complete",
            "audio_stage": "render",
            "selection_basis": "audio_understanding",
            "rendered_clips": len(rows),
            "sha256": {"manifest.json": sha(merged / "manifest.json")},
        },
    )
    publish(merged, "clipforge-result-" + parent)
    if errors:
        raise RuntimeError(
            "Some renders failed; partial results are saved for explicit render-only reuse"
        )
    print(
        "Parallel selected render completed in %.1f seconds"
        % (time.monotonic() - start),
        flush=True,
    )


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--stop"]:
            root = Path(os.environ["RUNNER_TEMP"]) / "parallel-selected-render"
            controller = Path(__file__).with_name("colab_actions.py")
            failures = []
            for state in root.glob("control-*"):
                if (state / "state.json").is_file():
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(controller),
                            "stop",
                            "--state-dir",
                            str(state),
                        ],
                        check=False,
                    )
                    if result.returncode:
                        failures.append(state.name)
                    else:
                        clear_stopped(state)
            if failures:
                raise RuntimeError(
                    "Release could not be confirmed for every owned runtime"
                )
        elif sys.argv[1:] == ["--collect-only"]:
            main(collect_only=True)
        elif sys.argv[1:] == ["--collect-partial"]:
            main(collect_only=True, partial=True)
        elif sys.argv[1:]:
            raise RuntimeError("Unexpected arguments")
        else:
            main()
    except Exception as error:
        print("Parallel render failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
