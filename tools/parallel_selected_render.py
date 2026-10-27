#!/usr/bin/env python3
"""Render saved audio-understanding clips with at most four owned Colab runtimes."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

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


def command(args, env):
    result = subprocess.run(args, env=env, check=False)
    if result.returncode:
        raise RuntimeError(
            "Parallel render worker failed; inspect its saved private request"
        )


def main():
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
    root.mkdir(mode=0o700)
    private_repo()
    controller = Path(__file__).with_name("colab_actions.py")
    worker_ids = [
        hashlib.sha256((parent + ":render:" + str(i)).encode()).hexdigest()[:32]
        for i in range(1, count + 1)
    ]

    def worker(index):
        identity = worker_ids[index - 1]
        tag = "clipforge-input-" + identity
        result_name = "clipforge-result-" + identity
        existing = release(result_name + "-run-" + run_id)
        if existing is not None:
            record = read_transfer(run_id, result_name)
            if record.get("request_receipt", {}).get("status") != "clip_rendered":
                raise RuntimeError("Saved partial render is not complete")
            return
        if release(tag) is not None:
            raise RuntimeError(
                "Render request was already attempted; inspect its private state"
            )
        env = {
            **os.environ,
            "REQUEST_ID": identity,
            "INPUT_TAG": tag,
            "CLIP_INDEX": str(index),
            "CLIPFORGE_SHARED_BALANCE": "1",
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
        state = root / ("control-" + str(index))
        output = root / ("receipt-" + str(index))
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

    with ThreadPoolExecutor(max_workers=min(4, count)) as pool:
        futures = [pool.submit(worker, i) for i in range(1, count + 1)]
        errors = []
        for future in futures:
            try:
                future.result()
            except Exception as error:
                errors.append(error)
    if errors:
        raise RuntimeError(
            "Some parallel renders failed; completed private results were retained"
        )
    merged = root / "merged"
    merged.mkdir(mode=0o700)
    (merged / "clips").mkdir(mode=0o700)
    rows = []
    gpu_parts = []
    for index, identity in enumerate(worker_ids, 1):
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
        shutil.move(str(part / expected), str(merged / expected))
        rows.extend(rendered)
        gpu_parts.append(
            {
                "clip": index,
                "request_id": identity,
                "receipt": transfer.get("gpu_receipt"),
            }
        )
        if index == 1:
            shutil.copytree(part / "audio-review", merged / "audio-review")
    manifest = {
        "status": "complete",
        "rendered": rows,
        "reused_clips": sum(row.get("reused") is True for row in rows),
        "newly_rendered_clips": sum(row.get("reused") is not True for row in rows),
        "editorial_revision": selection.get("editorial_revision"),
        "source_url": selection["source_url"],
        "full_source_asr": False,
        "timestamp_source": "gpu_asr",
        "full_video_quality_audit": "not_run",
        "parallel_render_workers": min(4, count),
    }
    write_json(merged / "manifest.json", manifest)
    write_json(
        merged / "gpu-receipt.json",
        {
            "mode": "parallel-audio-stage",
            "status": "complete",
            "human_review_performed": False,
            "parts": gpu_parts,
            "elapsed_seconds": round(time.monotonic() - start, 2),
        },
    )
    write_json(
        merged / "request-receipt.json",
        {
            "request_id": parent,
            "source_ref": source_ref,
            "workflow_run": run_id,
            "status": "complete",
            "audio_stage": "render",
            "selection_basis": "audio_understanding",
            "rendered_clips": count,
            "sha256": {"manifest.json": sha(merged / "manifest.json")},
        },
    )
    publish(merged, "clipforge-result-" + parent)
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
            if failures:
                raise RuntimeError(
                    "Release could not be confirmed for every owned runtime"
                )
        elif sys.argv[1:]:
            raise RuntimeError("Unexpected arguments")
        else:
            main()
    except Exception as error:
        print("Parallel render failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
