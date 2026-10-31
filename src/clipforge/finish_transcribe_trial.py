"""Finish a single already-transcribed topic, retaining its source identity."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .audio_understanding import (
    Client,
    clean_selected_clip,
    selected_transcript,
    save_selected_outputs,
)
from .media_sources import canonical
from .models import SelectedClip
from .storage import PipelineError, digest, fingerprint, write_json
from .streaming_render import StreamingRenderer


def finish_trial(url, output, settings, resume):
    if (
        not resume
        or os.environ.get("EDITORIAL_RELEASE") != "clipforge-kick-editorial-40545180185"
    ):
        raise PipelineError("Expected the saved transcription trial")
    original = json.loads((resume / "audio-editor-receipt.json").read_text())
    trial = json.loads((resume / "transcribe-trial.json").read_text())
    transcription = json.loads((resume / "transcribe-words.json").read_text())
    if (
        canonical(original.get("source_url", "")) != canonical(url)
        or trial.get("status") != "complete"
    ):
        raise PipelineError("Saved transcription source or status differs")
    if (
        trial.get("clip") != transcription.get("clip")
        or len(trial.get("chunks", [])) != 1
    ):
        raise PipelineError(
            "Saved selected topic differs or requires multi-chunk handling"
        )
    clip = SelectedClip.model_validate(trial["clip"])
    groups = []
    for word in transcription.get("words", []):
        start, end, text = word["start"], word["end"], word["text"]
        speaker = word.get("source_speaker")
        if (
            not groups
            or speaker != groups[-1]["source_speaker"]
            or start - groups[-1]["end"] > 1.2
            or end - groups[-1]["start"] > 8
            or len(groups[-1]["text"]) > 45
        ):
            groups.append(
                {"start": start, "end": end, "text": text, "source_speaker": speaker}
            )
        else:
            groups[-1]["end"] = max(groups[-1]["end"], end)
            groups[-1]["text"] += text
    raw = {"language": "ja", "segments": groups}
    write_json(output / "transcribe-selected-source.json", raw)
    client = Client(output)
    client.record.update(
        source_url=url,
        status="running",
        timestamp_source="gemini_transcribe",
        parent_editorial_release=os.environ["EDITORIAL_RELEASE"],
        transcribe_input=trial,
        transcribe_untimed=transcription.get("untimed", []),
    )
    client.save()
    renderer = None
    try:
        with tempfile.TemporaryDirectory(prefix="clipforge-trial-audio-") as tmp:
            result = subprocess.run(
                [
                    "gh",
                    "release",
                    "download",
                    os.environ["EDITORIAL_RELEASE"],
                    "--repo",
                    "nyu1791-collab/ai-model-workers",
                    "--pattern",
                    "transcribe-audio-000.ogg",
                    "--dir",
                    tmp,
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
            if result.returncode:
                raise PipelineError("Saved selected audio could not be retrieved")
            selected_audio = Path(tmp) / "transcribe-audio-000.ogg"
            audio_key = fingerprint(
                {
                    "parent": os.environ["EDITORIAL_RELEASE"],
                    "sha256": digest(selected_audio),
                }
            )
            value = clean_selected_clip(
                client, raw, 1, clip, [], selected_audio, audio_key
            )
        value["finishing"]["status"] = "transcribe_timestamps_preserved"
        value["finishing"]["timestamp_source"] = "gemini_transcribe"
        save_selected_outputs(output, url, [clip], [raw], [value], client)
        file = output / "selected-transcripts.json"
        data = json.loads(file.read_text())
        data["timestamp_source"] = "gemini_transcribe"
        write_json(file, data)
        client.record.update(
            status="complete",
            timestamp_method="transcribe_word_offsets",
            timestamp_alignment="not_forced_aligned",
        )
        client.save()
        if os.environ.get("CLIPFORGE_STREAMING_RENDER") == "true":
            renderer = StreamingRenderer(
                output,
                url,
                [clip],
                os.environ["CONFIG_FILE"],
                timestamp_source="gemini_transcribe",
            )
            transcript, turns = selected_transcript(value, clip.end - clip.start)
            renderer.enqueue(
                1,
                {
                    "transcript": transcript.model_dump(),
                    "turns": turns,
                    "sound_effects": value["sound_effects"],
                },
            )
            renderer.finish()
    except Exception:
        client.record["status"] = "failed"
        raise
    finally:
        if renderer is not None:
            renderer.close()
        client.record["finished_at"] = time.time()
        client.save()
