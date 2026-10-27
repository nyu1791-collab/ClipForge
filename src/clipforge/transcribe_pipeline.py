"""Selected-topic transcription with the configured Vertex identity."""

import http.client
import json
import socket
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from .gemini_audio_editor import PROJECT, audio_part, encode_audio
from .storage import PipelineError, fingerprint, write_json
from .transcribe_adapter import word_segments

MAX_SECONDS = 898.0
OVERLAP_SECONDS = 2.0
MAX_PARALLEL = 6
slots = BoundedSemaphore(MAX_PARALLEL)


def transcription_config():
    """The supported API settings, shared by request identity and payload."""
    return {
        "mode": "VERBATIM",
        "languageCodes": ["ja-JP"],
        "wordTimestamp": True,
        "diarization": True,
    }


def request_chunk(client, audio, clip, index, part, begin, length, source_sha):
    identity = {
        "source_audio_sha256": source_sha,
        "clip_start": clip.start,
        "clip_end": clip.end,
        "offset": begin,
        "duration": length,
        "model": "gemini-3.5-transcribe",
        "language": "ja-JP",
        "config": transcription_config(),
        "encoding": "ogg-opus-16000hz-mono-24k-voip-v1",
    }
    key = fingerprint(identity)
    phase = "transcribe-%03d-%03d-%s" % (index, part, key[:16])
    saved = client.resume / (phase + ".json") if client.resume else None
    rows = []
    if client.resume:
        receipt = json.loads((client.resume / "audio-editor-receipt.json").read_text())
        rows = [
            r
            for r in receipt.get("requests", [])
            if r.get("phase") == phase and not r.get("reused")
        ]
        successes = [
            r
            for r in receipt.get("requests", [])
            if r.get("phase") == phase and r.get("status") == "received"
        ]
        if successes and saved and saved.is_file():
            result = json.loads(saved.read_text())
            write_json(client.output / (phase + ".json"), result)
            with client._lock:
                client.record["requests"].append({**successes[-1], "reused": True})
                client.save()
            return result
        if not client.continue_missing:
            raise PipelineError(
                "No saved Transcribe response; continuation was not requested"
            )
        status = rows[-1].get("http_status") if rows else None
        if status is not None and status != 429 and not 500 <= status <= 599:
            raise PipelineError("Transcribe request is non-retryable")
        if (
            rows
            and rows[-1].get("status") == "failed"
            and rows[-1].get("retryable") is not True
        ):
            raise PipelineError(
                "Transcribe request failure is not known to be retryable"
            )
        if len(rows) >= 3:
            raise PipelineError("Transcribe request already reached three attempts")
        with client._lock:
            client.record["requests"].extend(rows)
            client.save()
    with tempfile.TemporaryDirectory(prefix="clipforge-transcribe-") as tmp:
        path = Path(tmp) / "audio.ogg"
        encode_audio(audio, path, clip.start + begin, length, "24k", speech_fast=True)
        data = json.dumps(
            {
                "contents": [{"role": "user", "parts": [audio_part(path)]}],
                "generationConfig": {
                    "audioTranscriptionConfig": transcription_config()
                },
            }
        ).encode()
        endpoint = f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/gemini-3.5-transcribe:generateContent"
        for attempt in range(len(rows) + 1, 4):
            row = {
                "phase": phase,
                "model": identity["model"],
                "identity": identity,
                "attempt": attempt,
                "status": "submitted",
                "started_at": time.time(),
            }
            with client._lock:
                client.record["requests"].append(row)
                client.record["new_generation_requests"] += 1
                client.save()
            tick = time.monotonic()
            try:
                request = urllib.request.Request(
                    endpoint,
                    data=data,
                    headers={
                        "Authorization": "Bearer " + client.vertex_token(),
                        "Content-Type": "application/json",
                    },
                )
                with slots, urllib.request.urlopen(request, timeout=1800) as response:
                    result = json.loads(response.read())
                candidate = (result.get("candidates") or [{}])[0]
                if candidate.get("finishReason") != "STOP":
                    raise PipelineError("Transcribe response did not complete")
                write_json(client.output / (phase + ".json"), result)
                row.update(
                    status="received",
                    usage=result.get("usageMetadata", {}),
                    finish_reason="STOP",
                )
                return result
            except urllib.error.HTTPError as error:
                row.update(
                    status="failed",
                    http_status=error.code,
                    retryable=error.code == 429 or 500 <= error.code <= 599,
                )
                if (error.code != 429 and not 500 <= error.code <= 599) or attempt == 3:
                    raise
            except (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                ConnectionError,
                TimeoutError,
                socket.timeout,
            ):
                row.update(status="failed", retryable=True)
                if attempt == 3:
                    raise
            except Exception:
                row.update(status="failed", retryable=False)
                raise
            finally:
                row.update(
                    elapsed_seconds=round(time.monotonic() - tick, 3),
                    finished_at=time.time(),
                )
                client.save()
            time.sleep(5 if attempt == 1 else 15)


def transcribe_clip(client, audio, clip, index, source_sha):
    duration = clip.end - clip.start
    starts = [0.0]
    while starts[-1] + MAX_SECONDS < duration:
        starts.append(starts[-1] + MAX_SECONDS - OVERLAP_SECONDS)
    with ThreadPoolExecutor(max_workers=min(len(starts), MAX_PARALLEL)) as pool:
        futures = [
            pool.submit(
                request_chunk,
                client,
                audio,
                clip,
                index,
                i,
                start,
                min(MAX_SECONDS, duration - start),
                source_sha,
            )
            for i, start in enumerate(starts)
        ]
        responses = [f.result() for f in futures]
    words, untimed = [], []
    for i, (start, result) in enumerate(zip(starts, responses)):
        rows, missing = word_segments(result, start, duration)
        left = start if i == 0 else start + OVERLAP_SECONDS / 2
        right = min(
            duration,
            start
            + min(MAX_SECONDS, duration - start)
            - (OVERLAP_SECONDS / 2 if i + 1 < len(starts) else 0),
        )
        words.extend(
            {**row, "chunk": i}
            for row in rows
            if left <= (row["start"] + row["end"]) / 2 < right
        )
        untimed.extend({"chunk": i, "text": text} for text in missing)
    words.sort(key=lambda row: (row["start"], row["end"]))
    groups = []
    for word in words:
        if (
            not groups
            or (word["chunk"], word.get("source_speaker")) != groups[-1]["source"]
            or word["start"] - groups[-1]["end"] > 1.2
            or word["end"] - groups[-1]["start"] > 8
            or len(groups[-1]["text"]) > 45
        ):
            groups.append(
                {
                    "start": word["start"],
                    "end": word["end"],
                    "text": word["text"],
                    "source": (word["chunk"], word.get("source_speaker")),
                }
            )
        else:
            groups[-1]["end"] = max(groups[-1]["end"], word["end"])
            groups[-1]["text"] += word["text"]
    return {
        "language": "ja",
        "segments": [{k: v for k, v in row.items() if k != "source"} for row in groups],
        "untimed": untimed,
    }
