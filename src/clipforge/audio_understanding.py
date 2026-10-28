"""Audio topic planning, selected-only GPU ASR and audio-grounded cleanup."""

import argparse
import hashlib
import http.client
import json
import math
import os
import random
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock

from . import asr, audio_first, pipeline
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
from .speaker_captions import SPEAKER_COLORS, validate_turns
from .storage import PipelineError, fingerprint, write_json
from .storage import digest
from .word_timing import preserve_word_timing
from .terminology import RESEARCH_INSTRUCTIONS, grounded_terms

WINDOW_SECONDS = 5400
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
EDITORIAL_RULES_FILE = PROMPT_DIR / "gemini_editorial_rulebook_ja.md"
SUBTITLE_POLICY = "meaning_preserving_cleanup_v1"
RENDER_SUBTITLE_POLICIES = {SUBTITLE_POLICY, "meaning_preserving_compact_v1"}
EDITORIAL_RENDER_VERSION = "editorial-render-v3"
TOPIC_RULES_FILE = PROMPT_DIR / "gemini_topics_ja.md"
SUBTITLE_RULES_FILE = PROMPT_DIR / "gemini_subtitles_ja.md"
TRANSCRIPTION_UNIT = "selected_topic"
MAX_MODEL_ATTEMPTS = 3


def rules_file_for_phase(phase):
    if phase.startswith("topics-"):
        return TOPIC_RULES_FILE
    if phase.startswith("transcript-"):
        return SUBTITLE_RULES_FILE
    return None


class LockedRequest(dict):
    """Serialize request-row updates with receipt snapshots across workers."""

    def __init__(self, lock, value):
        self._lock = lock
        super().__init__(value)

    def update(self, *args, **kwargs):
        with self._lock:
            return super().update(*args, **kwargs)

    def __setitem__(self, key, value):
        with self._lock:
            return super().__setitem__(key, value)


class Client:
    def __init__(self, output, resume=None, continue_missing=False):
        self.output, self.resume = output, resume
        self.continue_missing = continue_missing
        self._lock = RLock()
        self._vertex_token = os.environ.get("CLIPFORGE_VERTEX_ACCESS_TOKEN")
        self._vertex_token_at = time.monotonic()
        self.record = {
            "status": "running",
            "requests": [],
            "new_generation_requests": 0,
            "full_source_asr": False,
            "timestamp_source": "gemini_transcribe",
            "timings": {},
            "started_at": time.time(),
        }
        self.save()

    def save(self):
        with self._lock:
            write_json(self.output / "audio-editor-receipt.json", self.record)

    def vertex_token(self):
        """Refresh the same OIDC identity after a long selected-audio GPU job."""
        with self._lock:
            return self._vertex_token_locked()

    def _vertex_token_locked(self):
        if time.monotonic() - self._vertex_token_at < 2700:
            if self._vertex_token:
                return self._vertex_token
            raise PipelineError("Designated Vertex identity is not connected")
        control_python = os.environ.get("CLIPFORGE_COLAB_CONTROL_PYTHON", "")
        if not control_python or not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            raise PipelineError("Vertex identity needs reconnection after the GPU job")
        code = (
            "import google.auth; "
            "from google.auth.transport.requests import Request; "
            "c,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform']); "
            "c.refresh(Request()); print(c.token)"
        )
        try:
            refreshed = subprocess.run(
                [control_python, "-c", code], capture_output=True, text=True, timeout=90
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PipelineError("Designated Vertex identity refresh failed") from None
        if refreshed.returncode or not refreshed.stdout.strip():
            raise PipelineError("Designated Vertex identity refresh failed")
        self._vertex_token = refreshed.stdout.strip()
        self._vertex_token_at = time.monotonic()
        return self._vertex_token

    def generate(self, phase, prompt, audio=None, research=False, audio_source_key=None):
        rule_file = rules_file_for_phase(phase)
        # All director calls receive the common book, plus their stage rules.
        # A missing/empty book is a deployment error, never a silent prompt fallback.
        rule_files = [] if research else [EDITORIAL_RULES_FILE]
        if rule_file:
            rule_files.append(rule_file)
        rule_parts, rule_sources = [], []
        for path in rule_files:
            text = path.read_text(encoding="utf-8").strip()
            if phase.startswith("transcript-") and self.record.get("timestamp_source") == "gemini_transcribe":
                text = text.replace("GPU文字起こし", "音声文字起こし").replace("GPUの文字起こし", "音声文字起こし").replace("GPU時刻", "文字起こし時刻").replace("GPUが文字起こし", "Transcribeが文字起こし")
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
        audio_identity = [
            {"label": label, "sha256": digest(Path(path)), "bytes": Path(path).stat().st_size}
            for label, path in audio_items
        ]
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
        phase_base = phase
        model = (
            "gemini-3.5-flash"
            if research or phase_base.startswith("transcript-")
            else "gemini-3.8-flash"
        )
        thinking_level = "MINIMAL" if research else "MEDIUM"
        # The model is part of a subtitle response's identity: an earlier 3.8
        # response must not be reported as a 3.5 result even for the same text.
        phase_hash = (
            hashlib.sha256(
                f"{prompt_sha256}:{model}:{thinking_level}:{audio_source_key or json.dumps(audio_identity, sort_keys=True, ensure_ascii=False)}".encode()
            ).hexdigest()
            if phase_base.startswith("transcript-")
            else prompt_sha256
        )
        phase = f"{phase}-{phase_hash[:10]}"
        print(
            json.dumps({"phase": phase, "status": "checking_saved_response"}),
            flush=True,
        )
        prior_attempts = []
        if self.resume:
            saved = json.loads((self.resume / "audio-editor-receipt.json").read_text())
            entries = [
                r
                for r in saved["requests"]
                if r["phase"] == phase
                and r.get("finish_reason") == "STOP"
                and r.get("status") == "received"
                and (
                    r.get("prompt_sha256") == prompt_sha256
                    or (not rule_files and not r.get("prompt_sha256"))
                )
            ]
            if entries:
                if len(entries) != 1:
                    raise PipelineError("Ambiguous saved response for " + phase)
                value = json.loads((self.resume / (phase + ".json")).read_text())
                with self._lock:
                    self.record["requests"].append({**entries[0], "reused": True})
                write_json(self.output / (phase + ".json"), value)
                (self.output / (phase + ".prompt.txt")).write_text(
                    complete_prompt, encoding="utf-8"
                )
                self.save()
                print(json.dumps({"phase": phase, "status": "reused"}), flush=True)
                return value, entries[0].get("grounding", {})
            if phase_base.startswith("topics-"):
                # A subtitle-rule edit must not resend an already completed topic
                # selection. Reuse it when the topic instructions and actual user
                # request still match; retain the original prompt and its hashes.
                topic_source = next(
                    (
                        source
                        for source in rule_sources
                        if source["file"].endswith("gemini_topics_ja.md")
                    ),
                    None,
                )
                previous = []
                for row in saved["requests"]:
                    if (
                        not row.get("phase", "").startswith(phase_base + "-")
                        or row.get("finish_reason") != "STOP"
                        or row.get("status") != "received"
                    ):
                        continue
                    if topic_source not in row.get("prompt_rules_sources", []):
                        continue
                    name = row.get("prompt_file", "")
                    if not name or Path(name).name != name:
                        continue
                    prompt_path = self.resume / name
                    if not prompt_path.is_file():
                        continue
                    prior_prompt = prompt_path.read_text(encoding="utf-8")
                    if (
                        prior_prompt.partition("【Gemini user content】\n")[2]
                        == user_prompt
                    ):
                        previous.append((row, prior_prompt))
                if len(previous) == 1:
                    row, prior_prompt = previous[0]
                    previous_phase = row["phase"]
                    value = json.loads(
                        (self.resume / (previous_phase + ".json")).read_text()
                    )
                    with self._lock:
                        self.record["requests"].append(
                            {**row, "reused": True, "prior_rules_preserved": True}
                        )
                    write_json(self.output / (previous_phase + ".json"), value)
                    (self.output / row["prompt_file"]).write_text(
                        prior_prompt, encoding="utf-8"
                    )
                    self.save()
                    print(
                        json.dumps(
                            {"phase": previous_phase, "status": "reused_prior_topic"}
                        ),
                        flush=True,
                    )
                    return value, row.get("grounding", {})
            prior_attempts = [r for r in saved["requests"] if r.get("phase") == phase and not r.get("reused")]
            if not self.continue_missing:
                raise PipelineError("No complete saved response for " + phase)
            if prior_attempts and prior_attempts[-1].get("http_status") in (400, 401, 403, 404):
                raise PipelineError("Non-retryable Gemini request failure")
            with self._lock:
                self.record["requests"].extend(prior_attempts)
            self.save()
        print(json.dumps({"phase": phase, "status": "new_request"}), flush=True)
        token = self.vertex_token()
        prompt_path = self.output / (phase + ".prompt.txt")
        prompt_path.write_text(complete_prompt, encoding="utf-8")
        parts = [{"text": user_prompt}]
        for label, audio_path in audio_items:
            parts.append({"text": f"\n\n音声パート「{label}」:"})
            parts.append(audio_part(Path(audio_path)))
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "thinkingConfig": {"thinkingLevel": thinking_level},
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
        last_attempt = len(prior_attempts) + MAX_MODEL_ATTEMPTS
        for attempt in range(len(prior_attempts) + 1, last_attempt + 1):
            try:
                return self._generate_attempt(phase, model, thinking_level, rule_files, rule_sources, rule_text, prompt_path, prompt_sha256, audio_identity, audio_source_key, token, data, research, attempt)
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code <= 599
                if not retryable or attempt >= last_attempt:
                    raise
            except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionError, TimeoutError, socket.timeout):
                if attempt >= last_attempt:
                    raise
            print(json.dumps({"phase": phase, "status": "retrying", "next_attempt": attempt + 1}), flush=True)
            time.sleep((15 if attempt == len(prior_attempts) + 1 else 45) + random.uniform(0, 5))

    def _generate_attempt(self, phase, model, thinking_level, rule_files, rule_sources, rule_text, prompt_path, prompt_sha256, audio_identity, audio_source_key, token, data, research, attempt):
        row = LockedRequest(
            self._lock,
            {
                "phase": phase,
                "model": model,
                "thinking_level": thinking_level,
                "status": "submitted",
                "attempt": attempt,
                "started_at": time.time(),
            },
        )
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
            audio_inputs=audio_identity,
            audio_source_key=audio_source_key,
        )
        with self._lock:
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
                        response, self.output / (phase + f".attempt-{attempt}.sse.txt"), row, self.save
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
                capture_http_error(error, self.output / (phase + f".attempt-{attempt}.http-error.json"))
            )
            raise
        except Exception:
            row["status"] = "failed"
            raise
        finally:
            row.update(elapsed_seconds=time.monotonic() - tick, finished_at=time.time())
            self.save()


def selected_transcript(value, duration, offset=0):
    if (
        value.get("transcript_access") != "available"
        and value.get("audio_access") != "heard"
    ):
        raise PipelineError("Selected transcript is unavailable")
    rows, turns = [], []
    ordered = value.get("segments", [])
    if not isinstance(ordered, list):
        raise PipelineError("Expected a subtitle list")
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
        raw_words = row.get("words", [])
        words = []
        if isinstance(raw_words, list) and all(isinstance(w, dict) for w in raw_words):
            if all(
                isinstance(w.get("text"), str) and w["text"] and
                type(w.get("start")) in (int, float) and type(w.get("end")) in (int, float) and
                math.isfinite(w["start"]) and math.isfinite(w["end"]) and
                start <= w["start"] < w["end"] <= end
                for w in raw_words
            ) and "".join(w["text"] for w in raw_words) == text:
                words = [{"start": w["start"] + offset, "end": w["end"] + offset,
                          "text": w["text"]} for w in raw_words]
        rows.append(
            Segment(
                start=start + offset, end=end + offset, text=text, words=words, **annotation(row)
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
        return Transcript(language="ja", segments=[]), []
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
                words=members[0].words if len(members) == 1 else [],
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


def selected_gpu_transcripts(
    audio, clips, settings, output, resume, continue_missing, audio_sha256, on_clip=None
):
    """Reuse a completed selected-only GPU result or run the existing Colab ASR worker once."""
    intervals = [{"start": clip.start, "end": clip.end} for clip in clips]
    identity = {
        "source_audio_sha256": audio_sha256,
        "intervals": intervals,
        "recognizer": asr.MODEL,
        "language": settings.language or "ja",
        "selected_input_version": 2,
        "audio_preprocessing": asr.SELECTED_AUDIO_FILTER,
    }
    key = fingerprint(identity)
    plan_file = output / "selected-gpu-asr-plan.json"
    write_json(plan_file, {**identity, "input_key": key, "clips": intervals})
    result_file = output / "selected-gpu-asr.json"
    saved_file = resume / result_file.name if resume else None
    if saved_file and saved_file.is_file():
        result = json.loads(saved_file.read_text())
        if result.get("input_key") == key and result.get("status") == "complete":
            write_json(result_file, result)
            return result, True
        if result.get("input_key") != key:
            raise PipelineError("Saved GPU transcript has a different ASR input; do not resubmit automatically")

    control_python = os.environ.get("CLIPFORGE_COLAB_CONTROL_PYTHON", "")
    if not control_python or not Path(control_python).is_file():
        raise PipelineError("Selected GPU ASR requires the configured Colab controller")
    helper = Path(__file__).resolve().parents[2] / "tools" / "selected_gpu_asr.py"
    saved_state = resume / "selected-gpu-asr-state.json" if resume else None
    state = (
        json.loads(saved_state.read_text())
        if saved_state and saved_state.is_file()
        else {}
    )
    if state.get("input_key") and state["input_key"] != key:
        raise PipelineError("Saved GPU job has a different ASR input; do not resubmit automatically")
    if (
        state.get("input_key") == key
        and state.get("status") == "gpu_requests_or_unknown"
    ):
        raise PipelineError(
            "Parallel selected GPU requests were already attempted; inspect saved part states before recovery"
        )
    if state.get("input_key") == key and state.get("status") in {
        "gpu_requested_or_unknown",
        "complete",
    }:
        # Only retrieve the existing result; a missing response never starts another GPU job.
        # Preserve the original identity even if this read-only recovery also fails.
        write_json(output / "selected-gpu-asr-state.json", state)
        arguments = ["--recover", str(saved_state), "--output", str(output)]
        reused = True
    else:
        if resume and not continue_missing:
            raise PipelineError(
                "No saved GPU transcript; continue-missing was not requested"
            )
        if (
            state.get("input_key") == key
            and state.get("status") == "staging_or_unknown"
        ):
            raise PipelineError(
                "Selected GPU input staging is uncertain; inspect saved state"
            )
        arguments = [
            "--source",
            str(audio),
            "--plan",
            str(plan_file),
            "--output",
            str(output),
        ]
        reused = False
    process = subprocess.Popen([control_python, str(helper), *arguments])
    seen = set()
    callback_error = None
    while True:
        if on_clip is not None and callback_error is None:
            for path in sorted(output.glob("selected-gpu-clip-*.json")):
                try:
                    item = json.loads(path.read_text())
                    index = item.get("clip_index")
                    if (
                        item.get("status") != "complete"
                        or item.get("input_key") != key
                        or type(index) is not int
                        or not 1 <= index <= len(clips)
                        or not isinstance(item.get("transcript"), dict)
                    ):
                        raise PipelineError("Selected GPU clip identity differs")
                    if index not in seen:
                        on_clip(index, item["transcript"])
                        seen.add(index)
                except Exception as error:
                    callback_error = error
                    break
        try:
            returncode = process.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            continue
    if callback_error is not None:
        raise callback_error
    if returncode or not result_file.is_file():
        raise PipelineError(
            "Selected GPU ASR did not complete; saved state was retained"
        )
    result = json.loads(result_file.read_text())
    if result.get("status") != "complete" or result.get("input_key") != key:
        raise PipelineError("Selected GPU transcript belongs to a different input")
    return result, reused


def clean_selected_clip(client, raw, clip_index, clip, glossary, audio_path, audio_source_key=None):
    """Use the selected audio and GPU text, retaining the GPU's time spans."""
    duration = clip.end - clip.start
    source = raw.get("segments", [])
    if not isinstance(source, list):
        raise PipelineError("Selected GPU transcript is unavailable")
    if not source:
        return {
            "transcript_access": "available",
            "segments": [],
            "sound_effects": [],
            "finishing": {"status": "no_speech_recognized", "model_requests": 0},
        }
    numbered = [
        {"id": index, "start": row["start"], "end": row["end"], "text": row["text"],
         **({"gap_recovery": True} if row.get("gap_recovery") else {})}
        for index, row in enumerate(source, 1)
    ]
    request = {
        "subtitle_policy": SUBTITLE_POLICY,
        "task": "添付音声はこの採用区間の先頭を0秒とする。文字起こしと時刻を原音で照合し、要約せず整文し、声を聞き分けられる発話だけ匿名の話者IDを付ける。新たに全文を書き起こさず、曖昧な語は原文を保持する。開始・終了時刻は元IDからコードが引き継ぐ。",
        "topic_context": {"title": clip.title, "reason": clip.reason},
        "glossary": glossary,
        "source_segments": numbered,
        "required_output_shape": {
            "audio_access": "heard または unavailable",
            "segments": [
                {
                    "source_ids": [1],
                    "text": "元の発言を保持した整文後の字幕",
                    "omit_filler": False,
                    "omit_non_speech": False,
                    "speaker_id": "speaker_1 から speaker_6、または unknown",
                    "confidence": "声に基づく話者判定の確信度0から1",
                }
            ],
            "speaker_assignments": [
                {
                    "source_id": 1,
                    "speaker_id": "speaker_1 から speaker_6、または unknown",
                    "confidence": "声に基づく話者判定の確信度0から1",
                }
            ],
            "sound_effects": [
                {
                    "at": "元の字幕時刻に基づく秒数",
                    "kind": "soft_pop / light_chime / transition_tick",
                    "reason": "入力文に根拠がある理由",
                }
            ],
        },
        "rules": "source_idsは各字幕が使う連続した元IDの配列。小さい声、短い相槌、意味のある反復も聞こえた発話は残す。声の特徴を区間全体で比較し、各元IDについてspeaker_assignmentsを別に返す。短い声も前後の同じ声と比較し、音声だけで識別できる場合はIDを付ける。同じ話者と確認できない複数の発話を結合しない。声が重なる、1つの元IDに複数の声が混じる、区別できない場合はunknown。文脈だけで話者を決めず、名前や役割を推定しない。同じ声に同じIDを付け、IDはクリップ内の出現順とする。時刻を推測して分割しない。感情はneutral。",
        "gap_recovery_rules": "gap_recoveryがtrueの行は空白部分を再認識した候補です。原音に発話や意味のある声がなく非発話の誤認識だけと明確な場合は、その候補だけでsource_idsを作り、textを空、omit_non_speechをtrueにします。不確かなら保持し、他の発言と混ぜません。",
    }
    value, _ = client.generate(
        f"transcript-{clip_index}", json.dumps(request, ensure_ascii=False),
        audio=audio_path, audio_source_key=audio_source_key,
    )
    if value.get("audio_access") != "heard":
        raise PipelineError("Selected audio was not available to Gemini; not retried")

    # Use only contiguous input references; all unreferenced or ambiguous input is kept.
    # This is a preservation fallback, not an extra model call or quality gate.
    assignments = {}
    ambiguous = set()
    assignment_rows = value.get("speaker_assignments", [])
    if isinstance(assignment_rows, list):
        for item in assignment_rows:
            if not isinstance(item, dict):
                continue
            sid = item.get("source_id")
            if isinstance(sid, str) and sid.isascii() and sid.isdigit():
                sid = int(sid)
            if type(sid) is not int or not 1 <= sid <= len(source):
                continue
            if sid in assignments:
                ambiguous.add(sid)
                continue
            speaker, confidence = item.get("speaker_id"), item.get("confidence")
            certain = (isinstance(speaker, str) and speaker in SPEAKER_COLORS
                       and type(confidence) in (int, float) and math.isfinite(confidence)
                       and 0.8 <= confidence <= 1)
            assignments[sid] = (speaker, confidence) if certain else ("unknown", 0)
    for sid in ambiguous:
        assignments[sid] = ("unknown", 0)
    replacements, consumed = {}, set()
    candidates = value.get("segments", [])
    if not isinstance(candidates, list):
        candidates = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("source_ids"), list
        ):
            continue
        raw_ids = candidate["source_ids"]
        if any(
            not (type(i) is int or (isinstance(i, str) and i.isascii() and i.isdigit()))
            for i in raw_ids
        ):
            continue
        try:
            ids = [int(i) for i in raw_ids]
        except ValueError:
            continue
        if not ids or ids[0] < 1 or ids[-1] > len(source):
            continue
        if ids != list(range(ids[0], ids[-1] + 1)):
            continue
        if any(i in consumed for i in ids):
            continue
        spans = [source[i - 1] for i in ids]
        if any(b["start"] - a["end"] > 1.5 for a, b in zip(spans, spans[1:])):
            continue
        text = candidate.get("text")
        if (isinstance(text, str) and not text.strip() and candidate.get("omit_non_speech") is True
            and all(span.get("gap_recovery") is True for span in spans)):
            consumed.update(ids)
            replacements[ids[0]] = None
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = candidate.get("speaker_id")
        confidence = candidate.get("confidence")
        certain = (
            value.get("audio_access") == "heard"
            and isinstance(speaker, str)
            and speaker in SPEAKER_COLORS
            and type(confidence) in (int, float)
            and math.isfinite(confidence)
            and 0.8 <= confidence <= 1
        )
        # Per-source voice labels survive a missing or unusable edited segment.
        # Conflicting or unknown labels must not be merged into one voice.
        source_speakers = [assignments[i] for i in ids if i in assignments]
        distinct = {item[0] for item in source_speakers}
        if len(ids) > 1 and (len(distinct - {"unknown"}) > 1 or "unknown" in distinct):
            continue
        if "unknown" in distinct:
            certain = False
        if len(source_speakers) == len(ids) and len(distinct) == 1 and "unknown" not in distinct:
            speaker = source_speakers[0][0]
            confidence = min(item[1] for item in source_speakers)
            certain = True
        source_words = [word for span in spans for word in span.get("words", [])]
        preserved_words = preserve_word_timing(source_words, text)
        consumed.update(ids)
        replacements[ids[0]] = (
            {
                "start": spans[0]["start"],
                "end": spans[-1]["end"],
                "text": text,
                "words": preserved_words,
                "speaker_id": speaker if certain else "unknown",
                "confidence": confidence if certain else 0,
                "emotion": "neutral",
                "emotion_level": 0,
                "emotion_confidence": 0,
            }
            if text.strip()
            else None
        )
    segments = []
    for index, row in enumerate(source, 1):
        if index in replacements:
            if replacements[index] is not None:
                segments.append(replacements[index])
        elif index not in consumed:
            speaker, confidence = assignments.get(index, ("unknown", 0))
            segments.append(
                {
                    "start": row["start"],
                    "end": row["end"],
                    "text": row["text"],
                    "words": row.get("words", []),
                    "speaker_id": speaker,
                    "confidence": confidence,
                    "emotion": "neutral",
                    "emotion_level": 0,
                    "emotion_confidence": 0,
                }
            )
    return {
        "transcript_access": "available",
        "segments": segments,
        "sound_effects": prepare_cues(value.get("sound_effects", []), duration),
        "finishing": {"status": "gpu_timestamps_preserved", "model_requests": 1, "audio_access": value.get("audio_access", "unavailable")},
    }


def save_selected_outputs(output, url, chosen, gpu_clips, values, client):
    """Persist complete editing data before waiting for render completion."""
    selection, transcripts = [], []
    for index, (clip, value) in enumerate(zip(chosen, values), 1):
        transcript, turns = selected_transcript(value, clip.end - clip.start)
        client.record.setdefault("subtitle_display", []).append(
            {
                "clip": index,
                "source_segments": len(gpu_clips[index - 1].get("segments", [])),
                "display_segments": len(transcript.segments),
                "overlap_policy": "union_display_neutral_color_preserve_text",
                "transcription_unit": TRANSCRIPTION_UNIT,
                "editing_requests": value["finishing"]["model_requests"],
                "finishing": value["finishing"],
            }
        )
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
            "timestamp_source": client.record.get("timestamp_source", "gpu_asr"),
            "subtitle_policy": SUBTITLE_POLICY,
            "transcription_unit": TRANSCRIPTION_UNIT,
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
    client.record["editing_status"] = "complete"
    client.save()


def process_selected(audio, url, output, settings, resume, continue_missing, identity, client, chosen, glossary):
    """Start one transcription per topic; finish each ready topic independently."""
    from .transcribe_pipeline import transcribe_clip, recover_active_gaps
    from .streaming_render import StreamingRenderer
    from .gap_diagnostics import describe_gaps

    def advisory_gaps(path, duration, segments):
        try:
            return describe_gaps(path, duration, segments)
        except Exception:
            return {"status": "unavailable", "reason": "level_analysis_failed", "gaps": []}

    if resume:
        saved_receipt = json.loads((resume / "audio-editor-receipt.json").read_text())
        if saved_receipt.get("timestamp_source") != "gemini_transcribe":
            raise PipelineError("Saved production used a different transcription route; resume its original code")
    client.record["timestamp_source"] = "gemini_transcribe"
    client.save()
    renderer = None
    if os.environ.get("CLIPFORGE_STREAMING_RENDER") == "true":
        renderer = StreamingRenderer(output, url, chosen, os.environ["CONFIG_FILE"], timestamp_source="gemini_transcribe")
    started = time.monotonic()
    try:
        def process_one(index, clip):
            raw = transcribe_clip(client, audio, clip, index, identity["audio_sha256"])
            write_json(output / f"transcribe-selected-{index:03}.json", raw)
            with tempfile.TemporaryDirectory(prefix="clipforge-selected-audio-") as tmp:
                selected_audio = Path(tmp) / f"selected-{index:03}.ogg"
                encode_audio(audio, selected_audio, clip.start, clip.end - clip.start, "24k", speech_fast=True)
                key = fingerprint({"source_audio_sha256": identity["audio_sha256"], "start": clip.start,
                                   "end": clip.end, "encoding": "ogg-opus-16000hz-mono-24k-voip-v1"})
                levels = advisory_gaps(selected_audio, clip.end - clip.start, raw["segments"])
                write_json(output / f"transcribe-gap-levels-{index:03}.json", levels)
                raw, recovery = recover_active_gaps(client, audio, clip, index, identity["audio_sha256"], raw, levels)
                write_json(output / f"transcribe-gap-recovery-{index:03}.json", recovery)
                write_json(output / f"transcribe-selected-{index:03}.json", raw)
                value = clean_selected_clip(client, raw, index, clip, glossary, selected_audio, key)
            value["finishing"]["status"] = "transcribe_timestamps_preserved"
            value["finishing"]["timestamp_source"] = "gemini_transcribe"
            if renderer is not None:
                transcript, turns = selected_transcript(value, clip.end - clip.start)
                renderer.enqueue(index, {"transcript": transcript.model_dump(), "turns": turns,
                                         "sound_effects": value["sound_effects"]})
            return raw, value

        with ThreadPoolExecutor(max_workers=min(6, len(chosen))) as pool:
            futures = [pool.submit(process_one, index, clip) for index, clip in enumerate(chosen, 1)]
            results = [future.result() for future in futures]
        raws, values = zip(*results)
        client.record["selected_transcription"] = {"recognizer": "gemini-3.5-transcribe",
                                                   "selected_topics": len(raws), "full_source_asr": False}
        save_selected_outputs(output, url, chosen, raws, values, client)
        path = output / "selected-transcripts.json"
        saved = json.loads(path.read_text())
        saved["timestamp_source"] = "gemini_transcribe"
        write_json(path, saved)
        if renderer is not None:
            renderer.finish()
    finally:
        try:
            if renderer is not None:
                renderer.close()
        finally:
            client.record["timings"]["selected_pipeline_elapsed_seconds"] = round(time.monotonic() - started, 3)
            client.save()


def validate_reacquired_source(audio, resume, clips, saved_duration):
    """Compare short source samples to the saved overview before reusing intervals."""
    import numpy as np
    from .release_transfer import gh, private_repo

    tag = os.environ.get("EDITORIAL_RELEASE", "")
    if not __import__("re").fullmatch(r"clipforge-kick-editorial-(?:replay-)?[0-9]+", tag):
        raise PipelineError("Saved editorial identity is required for source verification")
    private_repo()
    windows = sorted({int((clip.start + 5) // WINDOW_SECONDS) for clip in clips})
    with tempfile.TemporaryDirectory(prefix="clipforge-source-match-") as temporary:
        ref_dir = Path(temporary)
        arguments = ["release", "download", tag, "--repo", "nyu1791-collab/ai-model-workers"]
        for window in windows:
            arguments.extend(["--pattern", f"overview-{window}.ogg"])
        gh(*arguments, "--dir", str(ref_dir))

        def decode(path, start):
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
                 "-t", "12", "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                capture_output=True, timeout=60,
            )
            if result.returncode:
                raise PipelineError("Source comparison could not decode audio")
            return np.frombuffer(result.stdout, dtype="<f4").astype(np.float64)

        matches = []
        for clip in clips:
            position = clip.start + 5
            window = int(position // WINDOW_SECONDS)
            original = decode(ref_dir / f"overview-{window}.ogg", position - window * WINDOW_SECONDS)
            current = decode(audio, position)
            size = min(len(original), len(current))
            if size < 8 * 8000:
                raise PipelineError("Source comparison has insufficient audio")
            x, y = original[:size], current[:size]
            x = x - x.mean()
            y = y - y.mean()
            denom = np.linalg.norm(x) * np.linalg.norm(y)
            if denom == 0:
                raise PipelineError("Source comparison cannot match silent samples")
            fftsize = 1 << ((2 * size - 1).bit_length())
            cross = np.fft.irfft(np.fft.rfft(x, fftsize) * np.conj(np.fft.rfft(y, fftsize)), fftsize)
            search = np.r_[cross[-8000:], cross[:8001]]
            best = int(np.argmax(np.abs(search)))
            correlation = float(abs(search[best]) / denom)
            offset = (best - 8000) / 8000
            if correlation < 0.35 or abs(offset) > 0.08:
                raise PipelineError("Reacquired source does not match saved topic audio")
            matches.append({"start": position, "correlation": round(correlation, 4), "offset_seconds": offset})
    return {"method": "decoded_audio_sample_comparison", "saved_duration": saved_duration, "samples": matches}


def reprocess_saved(audio, url, output, settings, resume):
    """Explicitly start fresh GPU ASR for the exact saved selected intervals."""
    if not resume:
        raise PipelineError("Reprocessing requires a saved editorial release")
    canonical(url)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / "audio-editor-receipt.json").exists():
        raise PipelineError("Already attempted; inspect the saved request")
    receipt = json.loads((resume / "audio-editor-receipt.json").read_text())
    selection = json.loads((resume / "final-selection.json").read_text())
    prior_asr = json.loads((resume / "selected-gpu-asr-plan.json").read_text())
    old_identity = receipt.get("input_identity", {})
    if (canonical(selection.get("source_url", "")) != canonical(url)
        or old_identity.get("source_url") != url
        or old_identity.get("settings") != settings.model_dump()
        or prior_asr.get("source_audio_sha256") != old_identity.get("audio_sha256")):
        raise PipelineError("Saved selection belongs to another source or configuration")
    clips = [SelectedClip.model_validate({k: row[k] for k in
             ("start", "end", "candidate_id", "title", "reason", "confidence")})
             for row in selection.get("clips", [])]
    if not clips or [{"start": c.start, "end": c.end} for c in clips] != prior_asr.get("clips"):
        raise PipelineError("Saved selected intervals differ from prior GPU input")
    duration = audio_first.audio_info(audio)["duration"]
    saved_duration = receipt.get("duration")
    if (not isinstance(saved_duration, (int, float)) or abs(duration - saved_duration) > 0.5
        or any(c.end > duration for c in clips)):
        raise PipelineError("Reacquired audio duration differs from saved source")
    current_sha = digest(audio)
    comparison = {"method": "sha256_match"} if current_sha == old_identity.get("audio_sha256") else validate_reacquired_source(audio, resume, clips, saved_duration)
    glossary = json.loads((resume / "terminology-glossary.json").read_text()).get("terms", [])
    identity = {"source_url": url, "audio_sha256": current_sha, "settings": settings.model_dump()}
    client = Client(output)
    client.record.update(source_url=url, duration=duration, settings=settings.model_dump(),
                         input_identity=identity,
                         reprocessed_selection={"parent_release": os.environ.get("EDITORIAL_RELEASE"),
                            "parent_audio_sha256": old_identity.get("audio_sha256"),
                            "source_comparison": comparison, "topic_count": len(clips)})
    client.save()
    try:
        process_selected(audio, url, output, settings, None, False, identity, client, clips, glossary)
        client.record.update(status="complete", timestamp_alignment="not_forced_aligned",
                             timestamp_method="transcribe_word_offsets", subtitle_policy=SUBTITLE_POLICY,
                             sound_effects_status="planned_not_rendered")
    except Exception:
        client.record["status"] = "failed"
        raise
    finally:
        client.record["finished_at"] = time.time()
        client.save()


def plan(audio, url, output, settings, resume=None, continue_missing=False, transcribe_trial=False):
    canonical(url)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / "audio-editor-receipt.json").exists():
        raise PipelineError(
            "Already attempted; use a separate output for saved-response replay"
        )
    duration = audio_first.audio_info(audio)["duration"]
    starts = list(range(0, math.ceil(duration), WINDOW_SECONDS))
    # A window in a new output directory has no verified link to this input.
    # Refuse it before any concurrent request can send the wrong audio; saved
    # responses use the separate --resume-from directory and input identity.
    for index in range(len(starts)):
        path = output / f"overview-{index}.ogg"
        if path.exists() or path.is_symlink():
            raise PipelineError("Overview output already exists; use a fresh output")
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
    overview_pool = ThreadPoolExecutor(max_workers=2)
    topic_pool = None
    try:
        topics = []

        def prepare_overview(index, start):
            path = output / f"overview-{index}.ogg"
            started = time.monotonic()
            started_at = time.time()
            length = min(WINDOW_SECONDS, duration - start)
            encode_audio(audio, path, start, length, "12k", speech_fast=True)
            return path, {
                "index": index,
                "source_start_seconds": start,
                "source_duration_seconds": length,
                "started_at": started_at,
                "finished_at": time.time(),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }

        overviews = {
            index: overview_pool.submit(prepare_overview, index, start)
            for index, start in enumerate(starts)
        }
        # Each audio window is independent. Start its request as soon as that
        # window is ready rather than waiting for previous model responses.
        # The limit is bounded and does not create extra windows or retries.
        cancelled = Event()

        def request_overview(index, start):
            length = min(WINDOW_SECONDS, duration - start)
            try:
                path, preparation = overviews[index].result()
                if cancelled.is_set():
                    raise PipelineError("Another overview request failed")
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
                return value, preparation
            except Exception:
                cancelled.set()
                raise

        topic_pool = ThreadPoolExecutor(max_workers=min(1 if resume else 2, len(starts)))
        topic_futures = {
            index: topic_pool.submit(request_overview, index, start)
            for index, start in enumerate(starts)
        }
        for index, start in enumerate(starts):
            length = min(WINDOW_SECONDS, duration - start)
            value, preparation = topic_futures[index].result()
            with client._lock:
                client.record["timings"].setdefault(
                    "overview_preparation_seconds", []
                ).append(preparation["elapsed_seconds"])
                client.record["timings"].setdefault("overview_windows", []).append(
                    preparation
                )
                client.save()
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
        if transcribe_trial:
            from .transcribe_trial import transcribe_topic

            transcribe_topic(audio, chosen[0], output, client)
            client.record.update(status="complete", timestamp_method="transcribe_word_offsets")
            return
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
        try:
            research, grounding = client.generate(
                "terminology",
                RESEARCH_INSTRUCTIONS + "\n配信URL:" + url + "\n話題資料:" + corpus,
                research=True,
            )
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise
            glossary, diagnostics = [], {"status": "unavailable", "reason": "http_error", "http_status": error.code}
        except PipelineError as error:
            if str(error) not in {
                "Incomplete Gemini response; not retried",
                "Expected JSON object",
                "Gemini returned no complete JSON object; raw response was preserved",
            }:
                raise
            glossary, diagnostics = [], {"status": "unavailable", "reason": type(error).__name__}
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError,
                TimeoutError, socket.timeout) as error:
            glossary, diagnostics = [], {"status": "unavailable", "reason": type(error).__name__}
        else:
            try:
                glossary, diagnostics = grounded_terms(research, grounding, corpus)
            except (ValueError, KeyError, TypeError) as error:
                glossary, diagnostics = [], {"status": "unavailable", "reason": type(error).__name__}
        write_json(
            output / "terminology-glossary.json",
            {"terms": glossary, "diagnostics": diagnostics},
        )
        process_selected(audio, url, output, settings, resume, continue_missing, identity, client, chosen, glossary)
        client.record.update(
            status="complete",
            timestamp_alignment="not_forced_aligned",
            timestamp_method="transcribe_word_offsets",
            subtitle_policy=SUBTITLE_POLICY,
            sound_effects_status="planned_not_rendered",
        )
    except Exception:
        client.record["status"] = "failed"
        raise
    finally:
        if topic_pool is not None:
            topic_pool.shutdown(wait=True, cancel_futures=True)
        overview_pool.shutdown(wait=True, cancel_futures=True)
        client.record["finished_at"] = time.time()
        client.save()


def render(review, transcripts_file, output, settings, clip_index=None):
    from .landscape import validate_selection

    if settings.chat_panel:
        raise PipelineError(
            "Audio-understanding route currently requires chat_panel=false"
        )
    settings = settings.model_copy(update={"video_encoder": "h264_nvenc"})
    value = json.loads(review.read_text())
    data = json.loads(transcripts_file.read_text())
    if data.get("subtitle_policy") in RENDER_SUBTITLE_POLICIES:
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
    if clip_index is not None and not 1 <= clip_index <= len(clips):
        raise PipelineError("Requested clip is outside the saved selection")
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
            if clip_index is not None and index != clip_index:
                continue
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
                "status": "clip_rendered" if clip_index is not None else "complete",
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
    parser.add_argument("--clip-index", type=int)
    parser.add_argument(
        "--clips", type=int, help="Advisory target count for topic selection"
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.clips is not None:
        config["clips"] = args.clips
    settings = Settings.model_validate(config)
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
        if args.config.name == "config.landscape-trial-finish.example.json":
            if not args.continue_missing:
                raise PipelineError("Finishing the transcription trial requires continue-missing")
            from .finish_transcribe_trial import finish_trial

            finish_trial(args.url, args.output, settings, args.resume_from)
        elif args.config.name == "config.landscape-reprocess.example.json":
            if not args.continue_missing:
                raise PipelineError("Selected-topic reprocessing must be explicitly requested")
            reprocess_saved(args.audio, args.url, args.output, settings, args.resume_from)
        else:
            plan(args.audio, args.url, args.output, settings, args.resume_from, args.continue_missing,
                 transcribe_trial=args.config.name == "config.landscape-transcribe-trial.example.json")
    else:
        try:
            render(
                args.review, args.transcripts, args.output, settings, args.clip_index
            )
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

