"""One selected topic: Vertex Transcribe trial, with no GPU or rendering."""
import json
import time
import urllib.request
from pathlib import Path

from .gemini_audio_editor import PROJECT, audio_part, encode_audio
from .storage import PipelineError, write_json
from .transcribe_adapter import word_segments


def transcribe_topic(audio, clip, output, client):
    duration = clip.end - clip.start
    width, overlap = 840.0, 2.0
    starts = [0.0]
    while starts[-1] + width < duration:
        starts.append(starts[-1] + width - overlap)
    record = {"model": "gemini-3.5-transcribe", "clip": clip.model_dump(),
              "status": "running", "chunks": [], "gpu_used": False}
    write_json(output / "transcribe-trial.json", record)
    all_words, untimed = [], []
    for index, begin in enumerate(starts):
        length = min(width, duration - begin)
        path = output / f"transcribe-audio-{index:03}.ogg"
        encode_audio(audio, path, clip.start + begin, length, "24k", speech_fast=True)
        body = {"contents": [{"role": "user", "parts": [audio_part(path)]}],
                "generationConfig": {"audioTranscriptionConfig": {
                    "languageCodes": ["ja-JP"], "mode": "VERBATIM",
                    "wordTimestamp": True, "diarization": True}}}
        row = {"chunk": index, "start": begin, "duration": length,
               "status": "submitted", "started_at": time.time()}
        record["chunks"].append(row)
        write_json(output / "transcribe-trial.json", record)
        req = urllib.request.Request(
            f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/gemini-3.5-transcribe:generateContent",
            data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + client.vertex_token(), "Content-Type": "application/json"})
        tick = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=1800) as response:
                value = json.loads(response.read())
            write_json(output / f"transcribe-response-{index:03}.json", value)
            words, missing = word_segments(value, begin, duration)
            left = begin if index == 0 else begin + overlap / 2
            right = min(duration, begin + length - (overlap / 2 if index + 1 < len(starts) else 0))
            all_words.extend({**word, "chunk": index} for word in words
                             if left <= (word["start"] + word["end"]) / 2 < right)
            untimed.extend({"chunk": index, "text": text} for text in missing)
            row.update(status="received", elapsed_seconds=round(time.monotonic() - tick, 3),
                       words=len(words), untimed_items=len(missing), usage=value.get("usageMetadata", {}))
        except Exception:
            row.update(status="failed_or_unknown", elapsed_seconds=round(time.monotonic() - tick, 3))
            record["status"] = "failed"
            raise
        finally:
            write_json(output / "transcribe-trial.json", record)
    all_words.sort(key=lambda row: (row["start"], row["end"]))
    write_json(output / "transcribe-words.json", {"clip": clip.model_dump(), "words": all_words,
               "untimed": untimed, "speaker_scope": "chunk", "timestamps": "estimated_clip_relative"})
    record["status"] = "complete"
    write_json(output / "transcribe-trial.json", record)
