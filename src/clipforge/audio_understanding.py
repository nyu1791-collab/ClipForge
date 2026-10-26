"""Audio topic planning and selected-only Gemini transcription, without full-source ASR."""

import argparse
import hashlib
import json
import math
import os
import shutil
from pathlib import Path
import time
import urllib.error
import urllib.request

from . import audio_first, pipeline
from .caption_emotions import annotation
from .gemini_audio_editor import (
    PROJECT,
    MAX_REQUEST_BYTES,
    audio_part,
    encode_audio,
    extract_json_object,
    collect_sse_response,
)
from .media_sources import canonical, open_source
from .models import Settings, Transcript, Segment, SelectedClip
from .sound_effects import prepare_cues
from .subtitle_finishing import merge_boundaries, snap_to_energy
from .speaker_captions import SPEAKER_COLORS, validate_turns
from .storage import PipelineError, fingerprint, write_json
from .storage import digest
from .terminology import RESEARCH_INSTRUCTIONS, grounded_terms

WINDOW_SECONDS = 5400
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
EDITORIAL_RULES_FILE = PROMPT_DIR / "gemini_editorial_rulebook_ja.md"
SUBTITLE_POLICY = "meaning_preserving_compact_v1"
EDITORIAL_RENDER_VERSION = "editorial-render-v3"
TOPIC_RULES_FILE = PROMPT_DIR / "gemini_topics_ja.md"
SUBTITLE_RULES_FILE = PROMPT_DIR / "gemini_subtitles_ja.md"
TRANSCRIPTION_CHUNK_SECONDS = 60
TRANSCRIPTION_CONTEXT_SECONDS = 1.5


def rules_file_for_phase(phase):
    if phase.startswith("topics-"):
        return TOPIC_RULES_FILE
    if phase.startswith("transcript-"):
        return SUBTITLE_RULES_FILE
    return None


class Client:
    def __init__(self, output, resume=None, continue_missing=False):
        self.output, self.resume = output, resume
        self.continue_missing = continue_missing
        self.record = {
            "status": "running",
            "requests": [],
            "new_generation_requests": 0,
            "full_source_asr": False,
            "timestamp_source": "gemini_estimated",
            "timings": {},
            "started_at": time.time(),
        }
        self.save()

    def save(self):
        write_json(self.output / "audio-editor-receipt.json", self.record)

    def generate(self, phase, prompt, audio=None, research=False):
        rule_file = rules_file_for_phase(phase)
        # All director calls receive the common book, plus their stage rules.
        # A missing/empty book is a deployment error, never a silent prompt fallback.
        rule_files = [] if research else [EDITORIAL_RULES_FILE]
        if rule_file:
            rule_files.append(rule_file)
        rule_parts, rule_sources = [], []
        for path in rule_files:
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise PipelineError("Required Gemini rulebook is empty: " + path.name)
            rule_parts.append(text)
            rule_sources.append(
                {
                    "file": str(path.relative_to(PROMPT_DIR.parent)),
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        rule_text = "\n\n".join(rule_parts)
        audio_items = []
        if audio:
            audio_items = audio if isinstance(audio, list) else [("添付音声", audio)]
        audio_manifest = "\n".join(
            f"音声パート{index}: {label}（{Path(audio_path).name}）"
            for index, (label, audio_path) in enumerate(audio_items, 1)
        )
        user_prompt = "\n\n".join(part for part in (prompt, audio_manifest) if part)
        complete_prompt = "\n\n".join(
            part
            for part in (
                "【Gemini systemInstruction】\n" + rule_text if rule_text else "",
                "【Gemini user content】\n" + user_prompt,
            )
            if part
        )
        prompt_sha256 = hashlib.sha256(complete_prompt.encode("utf-8")).hexdigest()
        # A changed ruleset or stage request gets a new response key. This lets
        # --continue-missing generate with the new instructions without colliding
        # with an already completed response from an older prompt.
        phase = f"{phase}-{prompt_sha256[:10]}"
        print(
            json.dumps({"phase": phase, "status": "checking_saved_response"}),
            flush=True,
        )
        if self.resume:
            saved = json.loads((self.resume / "audio-editor-receipt.json").read_text())
            entries = [
                r
                for r in saved["requests"]
                if r["phase"] == phase
                and r.get("finish_reason") == "STOP"
                and (
                    r.get("prompt_sha256") == prompt_sha256
                    or (not rule_files and not r.get("prompt_sha256"))
                )
            ]
            if entries:
                if len(entries) != 1:
                    raise PipelineError("Ambiguous saved response for " + phase)
                value = json.loads((self.resume / (phase + ".json")).read_text())
                self.record["requests"].append({**entries[0], "reused": True})
                write_json(self.output / (phase + ".json"), value)
                (self.output / (phase + ".prompt.txt")).write_text(
                    complete_prompt, encoding="utf-8"
                )
                self.save()
                print(json.dumps({"phase": phase, "status": "reused"}), flush=True)
                return value, entries[0].get("grounding", {})
            attempted = any(r["phase"] == phase for r in saved["requests"])
            if attempted or not self.continue_missing:
                raise PipelineError("No complete saved response for " + phase)
        print(json.dumps({"phase": phase, "status": "new_request"}), flush=True)
        token = os.environ.get("CLIPFORGE_VERTEX_ACCESS_TOKEN")
        if not token:
            raise PipelineError("Designated Vertex identity is not connected")
        model = "gemini-3.5-flash" if research else "gemini-3.8-flash"
        prompt_path = self.output / (phase + ".prompt.txt")
        prompt_path.write_text(complete_prompt, encoding="utf-8")
        parts = [{"text": user_prompt}]
        for label, audio_path in audio_items:
            parts.append({"text": f"\n\n音声パート「{label}」:"})
            parts.append(audio_part(Path(audio_path)))
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "thinkingConfig": {
                    "thinkingLevel": "MINIMAL" if research else "MEDIUM"
                },
                "maxOutputTokens": 8192 if research else 65536,
            },
        }
        if rule_text:
            body["systemInstruction"] = {
                "role": "system",
                "parts": [{"text": rule_text}],
            }
        if research:
            body["tools"] = [{"googleSearch": {}}]
        data = json.dumps(body, ensure_ascii=False).encode()
        if len(data) > MAX_REQUEST_BYTES:
            raise PipelineError("Bounded Gemini input exceeded")
        row = {
            "phase": phase,
            "model": model,
            "status": "submitted",
            "started_at": time.time(),
        }
        if rule_files:
            row.update(
                prompt_rules_file=" + ".join(source["file"] for source in rule_sources),
                prompt_rules_sources=rule_sources,
                prompt_rules_sha256=hashlib.sha256(
                    rule_text.encode("utf-8")
                ).hexdigest(),
            )
        row.update(
            prompt_file=prompt_path.name,
            prompt_sha256=prompt_sha256,
        )
        self.record["requests"].append(row)
        self.record["new_generation_requests"] += 1
        self.save()
        tick = time.monotonic()
        try:
            endpoint = f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/{model}:"
            endpoint += (
                "generateContent" if research else "streamGenerateContent?alt=sse"
            )
            req = urllib.request.Request(
                endpoint,
                data=data,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=1800) as response:
                if research:
                    result = json.loads(response.read())
                    candidate = result["candidates"][0]
                    text = "".join(
                        p.get("text", "")
                        for p in candidate.get("content", {}).get("parts", [])
                        if not p.get("thought")
                    )
                else:
                    result, text, _, _ = collect_sse_response(
                        response, self.output / (phase + ".sse.txt"), row, self.save
                    )
                    candidate = result["candidates"][0]
            row.update(
                finish_reason=candidate.get("finishReason"),
                usage=result.get("usageMetadata", {}),
                grounding=candidate.get("groundingMetadata", {}),
            )
            (self.output / (phase + ".raw.txt")).write_text(text)
            if row["finish_reason"] != "STOP":
                raise PipelineError("Incomplete Gemini response; not retried")
            value = extract_json_object(text)
            if not isinstance(value, dict):
                raise PipelineError("Expected JSON object")
            write_json(self.output / (phase + ".json"), value)
            row["status"] = "received"
            return value, row["grounding"]
        except urllib.error.HTTPError as error:
            from .http_diagnostics import capture_http_error

            row["status"] = "failed"
            row.update(
                capture_http_error(error, self.output / (phase + ".http-error.json"))
            )
            raise
        except Exception:
            row["status"] = "failed"
            raise
        finally:
            row.update(elapsed_seconds=time.monotonic() - tick, finished_at=time.time())
            self.save()


def selected_transcript(value, duration, offset=0):
    if value.get("audio_access") != "heard":
        raise PipelineError("Selected audio transcription is incomplete/unavailable")
    rows, turns = [], []
    ordered = value.get("segments", [])
    if not isinstance(ordered, list) or len(ordered) > 10000:
        raise PipelineError("Expected a bounded subtitle list")
    for index, row in enumerate(ordered):
        start, end = row.get("start"), row.get("end")
        # Normalize only enough for a renderable subtitle; do not stop production
        # on estimated timing, overlap, or an incomplete timing-ID list.
        fallback = duration * index / max(1, len(ordered))
        if type(start) not in (int, float) or not math.isfinite(start):
            start = fallback
        if type(end) not in (int, float) or not math.isfinite(end):
            end = start + 1
        start = max(0, min(start, max(0, duration - 0.01)))
        end = min(duration, max(start + 0.01, end))
        text = row.get("text")
        if not isinstance(text, str):
            raise PipelineError("Subtitle text is required")
        if not text.strip():
            continue
        rows.append(
            Segment(
                start=start + offset, end=end + offset, text=text, **annotation(row)
            )
        )
        speaker, confidence = row.get("speaker_id"), row.get("confidence", 0)
        certain = (
            isinstance(speaker, str)
            and speaker in SPEAKER_COLORS
            and type(confidence) in (int, float)
            and math.isfinite(confidence)
            and 0.8 <= confidence <= 1
        )
        turns.append(
            {
                "start": start,
                "end": end,
                "speaker_id": speaker if certain else None,
                "confidence": confidence if certain else 0,
            }
        )
    if not rows:
        raise PipelineError("No usable selected subtitles")
    # Keep both utterances when speakers talk over one another. Preserve the raw
    # response separately; overlapping display groups use neutral color and the union
    # of their original spans, without pretending to realign the audio.
    groups = []
    for row in sorted(rows, key=lambda r: (r.start, r.end)):
        if groups and row.start < groups[-1][-1].end:
            groups[-1].append(row)
            groups[-1].sort(key=lambda r: r.end)
        else:
            groups.append([row])
    display = []
    for members in groups:
        members.sort(key=lambda r: (r.start, r.end))
        display.append(
            Segment(
                start=min(r.start for r in members),
                end=max(r.end for r in members),
                text="\n".join(r.text for r in members),
                **(annotation(members[0].model_dump()) if len(members) == 1 else {}),
            )
        )
        if len(members) > 1:
            # Mark the complete display group uncertain so colors cannot imply one voice.
            turns.append(
                {
                    "start": display[-1].start - offset,
                    "end": display[-1].end - offset,
                    "speaker_id": None,
                    "confidence": 0,
                }
            )
    return Transcript(language="ja", segments=display), validate_turns(turns, duration)


def transcription_windows(duration):
    """Short target windows with nearby context; each utterance belongs to one core."""
    windows = []
    core_start = 0.0
    while core_start < duration:
        core_end = min(duration, core_start + TRANSCRIPTION_CHUNK_SECONDS)
        input_start = max(0.0, core_start - TRANSCRIPTION_CONTEXT_SECONDS)
        input_end = min(duration, core_end + TRANSCRIPTION_CONTEXT_SECONDS)
        windows.append(
            {
                "core_start": core_start,
                "core_end": core_end,
                "input_start": input_start,
                "input_duration": input_end - input_start,
                "core_start_local": core_start - input_start,
                "core_end_local": core_end - input_start,
            }
        )
        core_start = core_end
    return windows


def transcribe_selected_clip(
    client, output, audio_path, clip_index, clip, glossary, registry
):
    """Transcribe only a chosen clip, in short timestamp-local audio pieces."""
    duration = clip.end - clip.start
    segments, sound_effects = [], []
    for part_index, window in enumerate(transcription_windows(duration), 1):
        chunk_path = output / f"transcript-audio-{clip_index}-{part_index:03}.ogg"
        if not chunk_path.is_file():
            encode_audio(
                audio_path,
                chunk_path,
                window["input_start"],
                window["input_duration"],
                "24k",
            )
        audio_parts = [
            ("字幕対象音声（このパートだけを意味を保って短文化）", chunk_path)
        ]
        audio_parts.extend(
            (
                f"話者参照音声 {speaker['speaker_id']}（識別だけに使用）",
                output / speaker["file"],
            )
            for speaker in registry
        )
        request = {
            "subtitle_policy": SUBTITLE_POLICY,
            "task": "必読ルールブックに従い、原音の意味と口調に近い短い字幕と、必要な効果音指示を同じ応答で作る。",
            "topic_context": {"title": clip.title, "reason": clip.reason},
            "target_audio_duration": window["input_duration"],
            "target_time_range_to_transcribe": {
                "start_in_target_audio": window["core_start_local"],
                "end_in_target_audio": window["core_end_local"],
            },
            "time_range_on_selected_clip": {
                "start": window["core_start"],
                "end": window["core_end"],
            },
            "voice_registry": [
                {"speaker_id": entry["speaker_id"]} for entry in registry
            ],
            "glossary": glossary,
            "required_output_shape": {
                "audio_access": "heard",
                "complete": True,
                "segments": [
                    {
                        "start": "字幕対象音声パート内の秒数",
                        "end": "字幕対象音声パート内の秒数",
                        "text": "元発言の意味・口調・確かさを保つ、読みやすく短文化した表示字幕",
                        "speaker_id": "speaker_1〜speaker_6 または unknown",
                        "confidence": "0〜1",
                        "emotion": "neutral / surprise / joy / frustration / sadness / curiosity",
                        "emotion_level": "0=通常、1=控えめ、2=明確な感情の山だけ強め",
                        "emotion_confidence": "音声に基づく演出判断の確信度0〜1。不確かならneutral",
                    }
                ],
                "sound_effects": [
                    {
                        "at": "字幕対象音声パート内の秒数",
                        "kind": "soft_pop / light_chime / transition_tick",
                        "reason": "実際の発言に基づく採用理由。不要なら空配列",
                    }
                ],
            },
        }
        value, _ = client.generate(
            f"transcript-{clip_index}-{part_index:03}",
            json.dumps(request, ensure_ascii=False),
            audio_parts,
        )
        if value.get("audio_access") != "heard":
            raise PipelineError(
                "Selected audio transcription is incomplete/unavailable"
            )
        for cue in prepare_cues(
            value.get("sound_effects", []), window["input_duration"]
        ):
            if window["core_start_local"] <= cue["at"] < window["core_end_local"]:
                sound_effects.append(
                    {**cue, "at": round(cue["at"] + window["input_start"], 3)}
                )
        rows = value.get("segments", [])
        if not isinstance(rows, list):
            rows = []
        window_segments = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            start, end = row.get("start"), row.get("end")
            fallback_width = max(
                0.01, window["core_end_local"] - window["core_start_local"]
            )
            fallback = window["core_start_local"] + fallback_width * row_index / max(
                1, len(rows)
            )
            if type(start) not in (int, float) or not math.isfinite(start):
                start = fallback
            if type(end) not in (int, float) or not math.isfinite(end):
                end = start + 1.5
            # Keep context candidates until adjacent-window duplicates are reconciled.
            start = max(0.0, min(start, window["input_duration"] - 0.01))
            end = min(window["input_duration"], max(start + 0.01, end))
            text = row.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            mapped = {
                **row,
                "_window": part_index - 1,
                "_owned": window["core_start_local"]
                <= start
                < window["core_end_local"],
                "start": round(start + window["input_start"], 3),
                "end": round(end + window["input_start"], 3),
            }
            segments.append(mapped)
            window_segments.append(mapped)

        # Keep speaker references separate from the timestamped audio part. The next
        # Gemini call can identify a returning voice without adding seconds to its clock.
        known = {entry["speaker_id"] for entry in registry}
        for row in window_segments:
            speaker, confidence = row.get("speaker_id"), row.get("confidence", 0)
            if (
                speaker not in SPEAKER_COLORS
                or speaker in known
                or type(confidence) not in (int, float)
                or not math.isfinite(confidence)
                or confidence < 0.8
            ):
                continue
            sample_start = max(0.0, min(row["start"], duration - 0.01))
            sample_duration = min(4.0, duration - sample_start)
            if sample_duration < 0.5:
                continue
            name = f"voice-{speaker}.ogg"
            reference_path = output / name
            if not reference_path.is_file():
                encode_audio(
                    audio_path, reference_path, sample_start, sample_duration, "24k"
                )
            registry.append(
                {"speaker_id": speaker, "file": name, "duration": sample_duration}
            )
            known.add(speaker)

    segments, boundary_notes = merge_boundaries(
        segments, TRANSCRIPTION_CHUNK_SECONDS, TRANSCRIPTION_CONTEXT_SECONDS
    )
    segments, timing_notes = snap_to_energy(segments, audio_path, duration)
    return {
        "audio_access": "heard",
        "complete": True,
        "segments": segments,
        "sound_effects": prepare_cues(sound_effects, duration),
        "finishing": {"boundary_merge": boundary_notes, "timing_snap": timing_notes},
    }


def plan(audio, url, output, settings, resume=None, continue_missing=False):
    canonical(url)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / "audio-editor-receipt.json").exists():
        raise PipelineError(
            "Already attempted; use a separate output for saved-response replay"
        )
    duration = audio_first.audio_info(audio)["duration"]
    identity = {
        "source_url": url,
        "audio_sha256": digest(audio),
        "settings": settings.model_dump(),
    }
    if resume:
        saved_identity = json.loads(
            (resume / "audio-editor-receipt.json").read_text()
        ).get("input_identity")
        if identity != saved_identity:
            raise PipelineError(
                "Saved audio responses belong to another input/configuration"
            )
    client = Client(output, resume, continue_missing)
    client.record.update(
        source_url=url,
        duration=duration,
        settings=settings.model_dump(),
        input_identity=identity,
    )
    try:
        topics = []
        for index, start in enumerate(range(0, math.ceil(duration), WINDOW_SECONDS)):
            length = min(WINDOW_SECONDS, duration - start)
            path = output / f"overview-{index}.ogg"
            if not path.is_file():
                encode_audio(audio, path, start, length, "12k")
            value, _ = client.generate(
                f"topics-{index}",
                "今回の全体把握用音声窓について話題区間だけを返します。"
                f"この窓は元配信の{start}秒から{start + length}秒で、添付音声の長さは{length}秒です。"
                f"採用候補の長さの目安は{settings.min_seconds}〜{settings.max_seconds}秒ですが、短くても長くても自然な話題の区切りを優先してください。"
                "JSON {audio_access:'heard',topics:[{start:秒,end:秒,title,reason,confidence:0から1}]}。"
                "start/endは添付した今回の音声窓の先頭を0秒とする秒数です。",
                path,
            )
            if value.get("audio_access") != "heard":
                raise PipelineError("Overview audio unavailable")
            for row in value.get("topics", []):
                if not isinstance(row, dict):
                    continue
                try:
                    begin, finish = float(row["start"]), float(row["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(begin) or not math.isfinite(finish):
                    continue
                begin, finish = max(0, min(length, begin)), max(0, min(length, finish))
                if finish <= begin:
                    continue
                confidence = row.get("confidence", 0.5)
                if (
                    type(confidence) not in (int, float)
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                ):
                    confidence = 0.5
                clip = SelectedClip(
                    start=begin,
                    end=finish,
                    candidate_id=f"topic-{len(topics)}",
                    title=str(row.get("title") or "話題"),
                    reason=str(row.get("reason") or "音声から選定"),
                    confidence=confidence,
                )
                topics.append(
                    clip.model_copy(
                        update={"start": clip.start + start, "end": clip.end + start}
                    )
                )

        def choose_topics():
            selected = []
            for clip in sorted(topics, key=lambda c: (-c.confidence, c.start)):
                if all(clip.end <= c.start or clip.start >= c.end for c in selected):
                    selected.append(clip)
                if len(selected) == settings.clips:
                    break
            return sorted(selected, key=lambda c: c.start)

        chosen = choose_topics()
        margins = []
        padded = []
        for clip in chosen:
            start, end = max(0, clip.start - 0.3), min(duration, clip.end + 0.5)
            margins.append(
                {
                    "topic_start": clip.start,
                    "topic_end": clip.end,
                    "render_start": start,
                    "render_end": end,
                }
            )
            padded.append(clip.model_copy(update={"start": start, "end": end}))
        chosen = padded
        client.record["clip_safety_margins"] = margins
        if not chosen:
            raise PipelineError("No usable topic intervals were returned")
        client.record["topic_selection"] = {
            "requested_clips": settings.clips,
            "selected_clips": len(chosen),
            "count_target_met": len(chosen) == settings.clips,
            "duration_target_seconds": [settings.min_seconds, settings.max_seconds],
            "outside_duration_target": [
                {"start": clip.start, "end": clip.end, "seconds": clip.end - clip.start}
                for clip in chosen
                if not settings.min_seconds
                <= clip.end - clip.start
                <= settings.max_seconds
            ],
        }
        client.save()
        corpus = "\n".join(c.title + " " + c.reason for c in chosen)
        research, grounding = client.generate(
            "terminology",
            RESEARCH_INSTRUCTIONS + "\n配信URL:" + url + "\n話題資料:" + corpus,
            research=True,
        )
        glossary, diagnostics = grounded_terms(research, grounding, corpus)
        write_json(
            output / "terminology-glossary.json",
            {"terms": glossary, "diagnostics": diagnostics},
        )
        selection, transcripts, registry = [], [], []
        for index, clip in enumerate(chosen, 1):
            path = output / f"selected-{index}.ogg"
            if not path.is_file():
                encode_audio(audio, path, clip.start, clip.end - clip.start, "24k")
            value = transcribe_selected_clip(
                client,
                output,
                path,
                index,
                clip,
                glossary,
                registry,
            )
            transcript, turns = selected_transcript(value, clip.end - clip.start)
            client.record.setdefault("subtitle_display", []).append(
                {
                    "clip": index,
                    "source_segments": len(value["segments"]),
                    "display_segments": len(transcript.segments),
                    "overlap_policy": "union_display_neutral_color_preserve_text",
                    "transcription_window_seconds": TRANSCRIPTION_CHUNK_SECONDS,
                    "transcription_context_seconds": TRANSCRIPTION_CONTEXT_SECONDS,
                    "finishing": value["finishing"],
                }
            )
            client.save()
            selection.append(
                {
                    **clip.model_dump(),
                    "speaker_turns": turns,
                    "sound_effects": value["sound_effects"],
                }
            )
            transcripts.append(transcript.model_dump())
        write_json(
            output / "selected-transcripts.json",
            {
                "timestamp_source": "gemini_estimated",
                "subtitle_policy": SUBTITLE_POLICY,
                "transcription_window_seconds": TRANSCRIPTION_CHUNK_SECONDS,
                "transcription_context_seconds": TRANSCRIPTION_CONTEXT_SECONDS,
                "clips": transcripts,
            },
        )
        write_json(
            output / "final-selection.json",
            {
                "selection_basis": "audio_understanding",
                "subtitle_policy": SUBTITLE_POLICY,
                "source_url": url,
                "clips": selection,
            },
        )
        client.record.update(
            status="complete",
            timestamp_alignment="not_forced_aligned",
            timestamp_method="selected_audio_60_second_windows",
            subtitle_policy=SUBTITLE_POLICY,
            sound_effects_status="planned_not_rendered",
        )
    except Exception:
        client.record["status"] = "failed"
        raise
    finally:
        client.record["finished_at"] = time.time()
        client.save()


def render(review, transcripts_file, output, settings):
    from .landscape import validate_selection

    if settings.chat_panel:
        raise PipelineError(
            "Audio-understanding route currently requires chat_panel=false"
        )
    settings = settings.model_copy(update={"video_encoder": "h264_nvenc"})
    value = json.loads(review.read_text())
    data = json.loads(transcripts_file.read_text())
    if data.get("subtitle_policy") == SUBTITLE_POLICY:
        # Edited text has no word alignment; show the phrase as a whole and preserve it.
        settings = settings.model_copy(
            update={
                "caption_preserve_text": True,
                "caption_emotions": True,
                "caption": "plain",
            }
        )
    clips = validate_selection(
        {
            "clips": [
                {
                    k: v
                    for k, v in c.items()
                    if k not in {"speaker_turns", "sound_effects"}
                }
                for c in value["clips"]
            ]
        },
        settings,
        max(c["end"] for c in value["clips"]),
        advisory_targets=True,
    )
    if value.get("selection_basis") != "audio_understanding" or len(
        data["clips"]
    ) != len(clips):
        raise PipelineError("Mismatched selected transcripts")
    output.mkdir(parents=True, exist_ok=True)
    (output / "clips").mkdir(exist_ok=True)
    ranges = output.parent / "audio-understanding-source-ranges"
    vod = open_source(
        canonical(value["source_url"]),
        ranges,
        max_bytes=settings.max_transport_bytes,
        max_seconds=settings.max_source_seconds,
    )
    rows = []
    try:
        for index, (clip, raw) in enumerate(zip(clips, data["clips"]), 1):
            clip_settings = settings.model_copy(
                update={
                    "caption_emotions": settings.caption_emotions
                    or any(
                        annotation(segment)["emotion_level"] > 0
                        for segment in raw.get("segments", [])
                    )
                }
            )
            # Per-clip identity keeps an unrelated title/subtitle edit from invalidating
            # every other finished clip. Revision lineage is recorded separately.
            clip_key = fingerprint(
                {
                    "source_url": value["source_url"],
                    "clip": value["clips"][index - 1],
                    "transcript": raw,
                    "settings": clip_settings.model_dump(),
                    "subtitle_policy": data.get("subtitle_policy"),
                    "render_version": EDITORIAL_RENDER_VERSION,
                }
            )
            completed = output / "clips" / f"clip-{index:03}"
            if completed.is_symlink():
                raise PipelineError("Copied render directory must not be a symlink")
            if completed.exists():
                saved_path = completed / "render.json"
                saved = (
                    json.loads(saved_path.read_text()) if saved_path.is_file() else {}
                )
                if saved.get("editorial_input_sha256") == clip_key:
                    rows.append(
                        {
                            **pipeline._existing_clip(
                                completed, f"clip-{index:03}", saved, saved.get("key")
                            ),
                            "reused": True,
                        }
                    )
                    continue
                # This is the new job's copied render, never the source release.
                shutil.rmtree(completed)
            target = ranges / f"selected-{index}.mp4"
            target.parent.mkdir(exist_ok=True)
            receipt = audio_first.retrieved(
                vod, target, clip.start, clip.end, quality="360p"
            )
            if not 0 < receipt.get("rendition_height", 0) <= 360:
                raise PipelineError("Source must be at most 360p")
            offset = receipt["start_offset"]
            transcript = audio_first.shifted_transcript(
                Transcript.model_validate(raw), 0, clip.end - clip.start, offset=offset
            )
            local = clip.model_copy(
                update={"start": offset, "end": offset + clip.end - clip.start}
            )
            rows.append(
                pipeline.render_clip(
                    target,
                    output / "clips",
                    index,
                    local,
                    transcript,
                    clip_settings,
                    clip_key,
                    speaker_turns=value["clips"][index - 1]["speaker_turns"],
                    sound_effects=value["clips"][index - 1].get("sound_effects", []),
                )
            )
            rows[-1]["editorial_input_sha256"] = clip_key
            rows[-1]["editorial_render_version"] = EDITORIAL_RENDER_VERSION
            write_json(
                completed / "render.json",
                {k: v for k, v in rows[-1].items() if k != "directory"},
            )
        write_json(
            output / "manifest.json",
            {
                "status": "complete",
                "rendered": rows,
                "reused_clips": sum(row.get("reused") is True for row in rows),
                "newly_rendered_clips": sum(
                    row.get("reused") is not True for row in rows
                ),
                "editorial_revision": value.get("editorial_revision"),
                "editorial_render_version": EDITORIAL_RENDER_VERSION,
                "source_url": value["source_url"],
                "full_source_asr": False,
                "timestamp_source": data.get("timestamp_source", "gemini_estimated"),
                "full_video_quality_audit": "not_run",
            },
        )
    finally:
        vod.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["acquire", "plan", "render"])
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("config.landscape-long.example.json")
    )
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument(
        "--continue-missing",
        action="store_true",
        help="Reuse saved responses; generate only phases never submitted",
    )
    parser.add_argument("--review", type=Path)
    parser.add_argument("--transcripts", type=Path)
    args = parser.parse_args()
    settings = Settings.model_validate_json(args.config.read_text())
    if args.mode == "acquire":
        args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        vod = open_source(
            canonical(args.url),
            args.output / "transport",
            max_bytes=settings.max_transport_bytes,
            max_seconds=settings.max_source_seconds,
        )
        try:
            audio_first.acquire(vod, args.output)
        finally:
            vod.close()
    elif args.mode == "plan":
        plan(
            args.audio,
            args.url,
            args.output,
            settings,
            args.resume_from,
            args.continue_missing,
        )
    else:
        try:
            render(args.review, args.transcripts, args.output, settings)
        except Exception as error:
            pipeline.record_failure(args.output, error, "render")
            message = (
                str(error)[:240]
                if isinstance(error, PipelineError)
                else "Render failed"
            )
            message = __import__("re").sub(
                r"https?://\S+", "[source URL omitted]", message
            )
            write_json(
                args.output / "stage-error.json",
                {"error_type": type(error).__name__, "message": message},
            )
            raise


if __name__ == "__main__":
    main()
