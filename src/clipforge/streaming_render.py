"""Start a saved clip group's render after its selected GPU ASR and cleanup finish."""

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .github_cli import run as gh
from .gpu_slots import gpu_slot
from .release_transfer import private_repo, release
from .storage import PipelineError, write_json


def clip_request_id(parent, index):
    return hashlib.sha256((parent + ":render:" + str(index)).encode()).hexdigest()[:32]


def shard_request_id(parent, indices):
    key = ",".join(str(index) for index in sorted(indices))
    return hashlib.sha256((parent + ":render-batch:" + key).encode()).hexdigest()[:32]


def shard_editorial_tag(parent, indices):
    key = ",".join(str(index) for index in sorted(indices))
    digest = hashlib.sha256((parent + ":editorial-batch:" + key).encode()).hexdigest()[
        :32
    ]
    return "clipforge-kick-editorial-shard-" + digest


class StreamingRenderer:
    """Queue each ready clip on the next free GPU, shared with selected ASR."""

    def __init__(self, output, url, clips, settings, timestamp_source="gpu_asr"):
        self.output = output
        self.url = url
        self.clips = clips
        self.settings = settings
        self.timestamp_source = timestamp_source
        self.parent = os.environ["REQUEST_ID"]
        self.source_ref = os.environ["CLIPFORGE_SOURCE_REF"]
        self.run_id = os.environ["GITHUB_RUN_ID"]
        self.control_python = os.environ["CLIPFORGE_COLAB_CONTROL_PYTHON"]
        self.worker_count = min(4, len(clips))
        self.control_root = Path(os.environ["RUNNER_TEMP"]) / "parallel-selected-render"
        self.control_root.mkdir(mode=0o700, exist_ok=True)
        self.state_path = output / "streaming-render-state.json"
        self.lock = threading.Lock()
        self.stop_monitor = threading.Event()
        self.reported = set()
        self.enqueue_times = {}
        self.queue = queue.Queue()
        self.errors = []
        self.stopping = False
        self.workers = ThreadPoolExecutor(max_workers=self.worker_count)
        self.worker_futures = [
            self.workers.submit(self._consume, index)
            for index in range(self.worker_count)
        ]
        self.state = {
            "status": "running",
            "source_url": url,
            "source_ref": self.source_ref,
            "request_id": self.parent,
            "run_id": self.run_id,
            "groups": {},
            "started_at": time.time(),
        }
        self._save()
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def _save(self):
        write_json(self.state_path, self.state)

    def _mark(self, slot, **fields):
        with self.lock:
            self.state["groups"].setdefault(str(slot), {}).update(fields)
            self._save()

    def _monitor(self):
        while not self.stop_monitor.is_set():
            for index in range(1, len(self.clips) + 1):
                if index in self.reported or self.stop_monitor.is_set():
                    continue
                name = "clipforge-result-" + clip_request_id(self.parent, index)
                tag = name + "-run-" + self.run_id
                try:
                    info = subprocess.run(
                        [
                            "gh",
                            "api",
                            "repos/nyu1791-collab/ai-model-workers/releases/tags/"
                            + tag,
                        ],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    if info.returncode:
                        continue
                    assets = json.loads(info.stdout).get("assets", [])
                    uploaded = {
                        a.get("name") for a in assets if a.get("state") == "uploaded"
                    }
                    if not {"result.zip", "transfer.json"} <= uploaded:
                        continue
                    with tempfile.TemporaryDirectory() as tmp:
                        result = subprocess.run(
                            [
                                "gh",
                                "release",
                                "download",
                                tag,
                                "--repo",
                                "nyu1791-collab/ai-model-workers",
                                "--pattern",
                                "transfer.json",
                                "--dir",
                                tmp,
                            ],
                            capture_output=True,
                            timeout=5,
                            check=False,
                        )
                        if result.returncode:
                            continue
                        record = json.loads((Path(tmp) / "transfer.json").read_text())
                    receipt = record.get("request_receipt", {})
                    if (
                        record.get("run_id") != self.run_id
                        or record.get("name") != name
                        or receipt.get("request_id")
                        != clip_request_id(self.parent, index)
                        or receipt.get("source_ref") != self.source_ref
                        or receipt.get("status") != "clip_rendered"
                    ):
                        continue
                    self.reported.add(index)
                    started = (record.get("gpu_receipt") or {}).get("started_at")
                    elapsed = (
                        "%.1f秒" % max(0, time.time() - started)
                        if isinstance(started, (int, float))
                        else "不明"
                    )
                    print(
                        "[%d/%d 完了] clip-%03d (保存確認まで: %s)"
                        % (len(self.reported), len(self.clips), index, elapsed),
                        flush=True,
                    )
                except (OSError, TypeError, ValueError, subprocess.TimeoutExpired):
                    continue
            self.stop_monitor.wait(10)

    def enqueue(self, index, value):
        with self.lock:
            self.enqueue_times[index] = time.monotonic()
        self.queue.put((index, value))

    def _consume(self, worker_index):
        while True:
            item = self.queue.get()
            if item is None:
                self.queue.task_done()
                return
            control_state = self.control_root / (
                "control-" + str(worker_index) + "-clip-" + str(item[0])
            )
            items = [item]
            started = time.monotonic()
            with self.lock:
                queued = self.enqueue_times.pop(item[0], started)
            prepared = None
            phase = "preparing"
            try:
                # The private editorial data and request marker do not need a
                # GPU. Publish them while selected ASR may still own all slots.
                prepared = self.prepare(dict(items))
                prepared_at = time.monotonic()
                phase = "waiting_for_gpu"
                with gpu_slot("render", control_state) as lease:
                    now = time.monotonic()
                    phase = "running"
                    print(
                        json.dumps(
                            {
                                "stage": "render",
                                "clips": [index for index, _ in items],
                                "gpu_slot": lease.slot_index + 1,
                                "status": "starting",
                                "queue_wait_seconds": round(started - queued, 2),
                                "preparation_seconds": round(prepared_at - started, 2),
                                "gpu_slot_wait_seconds": round(now - prepared_at, 2),
                            }
                        ),
                        flush=True,
                    )
                    self.render(worker_index, prepared, control_state, lease)
                    print(
                        json.dumps(
                            {
                                "stage": "render",
                                "clips": [item[0]],
                                "gpu_slot": lease.slot_index + 1,
                                "status": "finished",
                                "slot_elapsed_seconds": round(
                                    time.monotonic() - now, 2
                                ),
                            }
                        ),
                        flush=True,
                    )
            except Exception as error:
                with self.lock:
                    self.errors.append(type(error).__name__)
                if prepared is not None:
                    # Do not overwrite gpu_requested_or_unknown: the controller
                    # may have allocated a runtime whose release is unconfirmed.
                    self._mark(
                        prepared["identity"],
                        failed_phase=phase,
                        error_type=type(error).__name__,
                        failed_at=time.time(),
                    )
                print(
                    json.dumps(
                        {
                            "stage": "render",
                            "clips": [index for index, _ in items],
                            "status": "failed",
                            "phase": phase,
                            "error_type": type(error).__name__,
                            "elapsed_seconds": round(time.monotonic() - started, 2),
                        }
                    ),
                    flush=True,
                )
            finally:
                for _ in items:
                    self.queue.task_done()

    def finish(self):
        if not self.stopping:
            self.stopping = True
            for _ in range(self.worker_count):
                self.queue.put(None)
        for future in self.worker_futures:
            future.result()
        self.workers.shutdown(wait=True)
        with self.lock:
            if self.state["status"] == "running":
                self.state.update(
                    status="failed" if self.errors else "complete",
                    finished_at=time.time(),
                    error_types=list(self.errors),
                )
                self._save()
        if self.errors:
            raise PipelineError(
                "Some render batches failed; completed clips remain saved"
            )

    def close(self):
        try:
            self.finish()
        finally:
            self.stop_monitor.set()
            self.monitor.join(timeout=10)

    def prepare(self, completed):
        """Save a single private shard before waiting for an owned GPU slot."""
        indices = sorted(completed)
        if not 1 <= len(indices) <= 2 or any(
            not 1 <= index <= len(self.clips) for index in indices
        ):
            raise PipelineError("Streaming render batch is invalid")
        identity = shard_request_id(self.parent, indices)
        editorial = shard_editorial_tag(self.parent, indices)
        marker = "clipforge-input-" + identity
        group = self.output / ("stream-render-" + identity)
        group.mkdir(mode=0o700)
        selection, transcripts = [], []
        for index, clip in enumerate(self.clips, 1):
            if index in completed:
                value = completed[index]
                selection.append(
                    {
                        **clip.model_dump(),
                        "speaker_turns": value["turns"],
                        "sound_effects": value["sound_effects"],
                    }
                )
                transcripts.append(value["transcript"])
            else:
                selection.append(
                    {**clip.model_dump(), "speaker_turns": [], "sound_effects": []}
                )
                transcripts.append({"language": "ja", "segments": []})
        review = {
            "selection_basis": "audio_understanding",
            "subtitle_policy": "meaning_preserving_cleanup_v1",
            "source_url": self.url,
            "clips": selection,
        }
        transcript_data = {
            "timestamp_source": self.timestamp_source,
            "subtitle_policy": "meaning_preserving_cleanup_v1",
            "transcription_unit": "selected_topic",
            "clips": transcripts,
        }
        receipt = {
            "status": "complete",
            "scope": "streaming_render_shard",
            "source_url": self.url,
            "request_id": self.parent,
            "clip_indices": indices,
        }
        for name, data in (
            ("final-selection.json", review),
            ("selected-transcripts.json", transcript_data),
            ("audio-editor-receipt.json", receipt),
        ):
            write_json(group / name, data)
        self._mark(
            identity,
            status="publishing_or_unknown",
            request_id=identity,
            editorial_release=editorial,
            clip_indices=indices,
        )
        private_repo()
        if release(editorial) is not None or release(marker) is not None:
            raise PipelineError(
                "Streaming shard was already attempted; inspect saved requests"
            )
        gh(
            "release",
            "create",
            editorial,
            str(group / "final-selection.json"),
            str(group / "selected-transcripts.json"),
            str(group / "audio-editor-receipt.json"),
            "--repo",
            "nyu1791-collab/ai-model-workers",
            "--latest=false",
            "--notes",
            "Private selected-topic render shard.",
            timeout=600,
        )
        gh(
            "release",
            "create",
            marker,
            "--repo",
            "nyu1791-collab/ai-model-workers",
            "--latest=false",
            "--notes",
            "Private render shard request marker.",
            timeout=600,
        )
        self._mark(identity, status="ready_for_gpu", prepared_at=time.time())
        return {
            "indices": indices,
            "identity": identity,
            "editorial": editorial,
            "marker": marker,
            "group": group,
        }

    def render(self, worker_index, prepared, control_state, lease):
        indices = prepared["indices"]
        identity = prepared["identity"]
        editorial = prepared["editorial"]
        marker = prepared["marker"]
        group = prepared["group"]
        self._mark(identity, status="gpu_requested_or_unknown")
        control = Path(__file__).resolve().parents[2] / "tools" / "colab_actions.py"
        output = self.control_root / ("receipt-" + identity)
        env = {
            **os.environ,
            "AUDIO_STAGE": "render",
            "PREVIOUS_RUN": self.run_id,
            "PREVIOUS_REQUEST": self.parent,
            "REQUEST_ID": identity,
            "INPUT_TAG": marker,
            "RESULT_STORE": "release",
            "REVIEW_FILE": str(group / "final-selection.json"),
            "CONFIG_FILE": self.settings,
            "CLIP_INDEX": "",
            "CLIPFORGE_RENDER_SHARD": ",".join(str(index) for index in indices),
            "CLIPFORGE_PARENT_REQUEST": self.parent,
            "CLIPFORGE_AUDIO_REVIEW_RELEASE": editorial,
            "CLIPFORGE_RESUME_RENDER": "false",
            "CLIPFORGE_SHARED_BALANCE": "1",
            "CLIPFORGE_ALLOCATION_WAIT_SECONDS": "600",
        }

        def command(*args):
            result = subprocess.run(
                [self.control_python, str(control), *args], env=env, check=False
            )
            if result.returncode:
                raise PipelineError(
                    "Streaming render shard failed; inspect its saved state"
                )

        try:
            lease.mark_requested()
            command("start", "--mode", "audio-stage", "--state-dir", str(control_state))
            command("run", "--state-dir", str(control_state), "--output", str(output))
        finally:
            if (control_state / "state.json").exists():
                command("stop", "--state-dir", str(control_state))
            lease.confirm_release()
        self._mark(identity, status="complete", finished_at=time.time())
