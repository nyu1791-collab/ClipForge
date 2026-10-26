"""Selected topics -> existing GPU Parakeet -> text-only Gemini edits with inherited clocks."""

import argparse
import json
import math
import os
from pathlib import Path
import re

from .audio_first import shifted_transcript
from .audio_understanding import Client, SUBTITLE_POLICY
from .models import Transcript
from .storage import PipelineError, digest, process, write_json


def prepare(audio, review, output, source_url, source_ref, request_id):
    selection = json.loads(review.read_text())
    if selection.get("source_url") != source_url or not selection.get("clips"):
        raise PipelineError("Saved selection belongs to another source or has no topics")
    if not re.fullmatch("[a-f0-9]{40}", source_ref) or not re.fullmatch(
        "[a-f0-9]{32}", request_id
    ):
        raise PipelineError("Invalid selected ASR identity")
    output.mkdir(parents=True, mode=0o700)
    filters, mapping, cursor = [], [], 0.0
    for index, clip in enumerate(selection["clips"]):
        start, end = float(clip["start"]), float(clip["end"])
        if not all(map(math.isfinite, (start, end))) or not 0 <= start < end:
            raise PipelineError("Invalid selected topic interval")
        filters.append(
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]"
        )
        mapping.append(
            {"clip": clip, "start": cursor, "end": cursor + end - start}
        )
        cursor += end - start
    filters.append(
        "".join(f"[a{i}]" for i in range(len(mapping)))
        + f"concat=n={len(mapping)}:v=0:a=1[out]"
    )
    target = output / "audio.wav"
    process(
        [
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(audio),
            "-filter_complex", ";".join(filters), "-map", "[out]",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(target),
        ],
        timeout=1200,
        stage="selected-topic-audio",
    )
    write_json(
        output / "selected-audio-map.json",
        {"source_url": source_url, "topics": mapping, "selected_seconds": cursor},
    )
    write_json(
        output / "request.json",
        {
            "request_id": request_id,
            "source_ref": source_ref,
            "audio_sha256": digest(target),
            "audio_bytes": target.stat().st_size,
            "language": "ja",
            "asr_part": {
                "index": 1, "parts": 1, "start": 0, "end": cursor,
                "selection_only": True, "topics": len(mapping),
            },
        },
    )


def inherit_edits(transcript, response):
    """Use only source IDs for timing; preserve source rows when an edit cannot map."""
    source = [s.model_dump() for s in transcript.segments]
    ids = {f"s{i:06}": i for i in range(len(source))}
    groups, used = {}, set()
    edits = response.get("segments", [])
    if not isinstance(edits, list):
        edits = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("text"), str):
            continue
        names = edit.get("source_ids")
        if not isinstance(names, list) or not names or any(
            not isinstance(n, str) or n not in ids for n in names
        ):
            continue
        indices = [ids[n] for n in names]
        if indices != list(range(indices[0], indices[-1] + 1)):
            continue
        if used.intersection(indices) or not edit["text"].strip():
            continue
        groups[indices[0]] = (indices, edit["text"])
        used.update(indices)
    rows, index = [], 0
    while index < len(source):
        if index not in groups:
            rows.append(source[index])
            index += 1
            continue
        indices, text = groups[index]
        rows.append(
            {
                "start": source[indices[0]]["start"],
                "end": source[indices[-1]]["end"],
                "text": text,
                "words": [],
                "emotion": "neutral",
                "emotion_level": 0,
                "emotion_confidence": 0,
            }
        )
        index = indices[-1] + 1
    return Transcript(language=transcript.language, segments=rows), len(groups)


def edit(transcript_file, mapping_file, output, parent_release):
    mapping = json.loads(mapping_file.read_text())
    raw = Transcript.model_validate_json(transcript_file.read_text())
    if not raw.segments:
        raise PipelineError("GPU ASR returned no transcript")
    output.mkdir(parents=True, mode=0o700)
    client = Client(output)
    client.record.update(
        source_url=mapping["source_url"],
        parent_release=parent_release,
        pipeline="selected_gpu_asr_then_text_edit",
        full_source_asr=False,
        timestamp_source="parakeet_inherited",
        timestamp_method="source_segment_ids",
        audio_sent_to_models=False,
        new_topic_selection_requests=0,
        new_terminology_requests=0,
        voice_identification_performed=False,
    )
    client.save()
    write_json(output / "gpu-asr-transcript.json", raw.model_dump())
    write_json(output / "selected-audio-map.json", mapping)
    selections, transcripts = [], []
    try:
        for index, topic in enumerate(mapping["topics"], 1):
            source = shifted_transcript(raw, topic["start"], topic["end"])
            if not source.segments:
                raise PipelineError("No GPU ASR segments for a selected topic")
            rows = [
                {
                    "id": f"s{i:06}",
                    "text": segment.text,
                    "duration": round(segment.end - segment.start, 3),
                }
                for i, segment in enumerate(source.segments)
            ]
            request = {
                "task": (
                    "採用した一話題のGPU文字起こしを、意味・口調・数字・否定・"
                    "確かさを保ち読みやすい整文テロップへ編集する。音声は添付していない。"
                    "人物や声質、声の感情、効果音を新たに推定しない。"
                    "自然な発言のまとまりを優先し5〜10秒程度を目安にするが、"
                    "固定長で無理に分割しない。隣接する元IDをまとめてよい。"
                    "返すのはsegments:[{source_ids:[元ID,...],text:整文}]のみ。"
                    "start/endや新しい時刻を生成しない。返さなかった元IDは原文を残す。"
                ),
                "topic": {
                    "title": topic["clip"].get("title", ""),
                    "reason": topic["clip"].get("reason", ""),
                },
                "segments": rows,
            }
            value, _ = client.generate(
                f"asr-text-edit-{index}", json.dumps(request, ensure_ascii=False)
            )
            edited, count = inherit_edits(source, value)
            clip = dict(topic["clip"])
            clip["speaker_turns"], clip["sound_effects"] = [], []
            selections.append(clip)
            transcripts.append(edited.model_dump())
            client.record.setdefault("text_edits", []).append(
                {
                    "clip": index, "source_segments": len(source.segments),
                    "display_segments": len(edited.segments),
                    "mapped_edit_groups": count,
                    "clock": "first_and_last_source_id",
                }
            )
            client.save()
            print(json.dumps({"clip": index, "status": "text_edit_complete"}), flush=True)
        write_json(
            output / "selected-transcripts.json",
            {
                "subtitle_policy": SUBTITLE_POLICY,
                "timestamp_source": "parakeet_inherited",
                "timestamp_method": "source_segment_ids",
                "clips": transcripts,
            },
        )
        write_json(
            output / "final-selection.json",
            {
                "selection_basis": "audio_understanding",
                "source_url": mapping["source_url"],
                "clips": selections,
            },
        )
        client.record.update(status="complete", timestamp_alignment="gpu_asr_inherited")
    except Exception:
        client.record["status"] = "failed"
        raise
    finally:
        client.save()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--audio", type=Path, required=True)
    prep.add_argument("--review", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--url", required=True)
    prep.add_argument("--source-ref", required=True)
    prep.add_argument("--request-id", required=True)
    refine = sub.add_parser("edit")
    refine.add_argument("--transcript", type=Path, required=True)
    refine.add_argument("--mapping", type=Path, required=True)
    refine.add_argument("--output", type=Path, required=True)
    refine.add_argument("--parent-release", required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(
            args.audio, args.review, args.output, args.url,
            args.source_ref, args.request_id,
        )
    else:
        edit(
            args.transcript, args.mapping, args.output, args.parent_release
        )


if __name__ == "__main__":
    main()

