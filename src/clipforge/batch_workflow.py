"""Shared, locked stage A for shorts and normal topic videos."""

import contextlib
import fcntl
import json
import tempfile
from pathlib import Path

from .batch import plan, run, transcribe_audio
from .batch_models import RichStyle
from .kick_media import KickVOD, canonical
from .models import Transcript
from .storage import PipelineError, digest, fingerprint, process, stage, write_json


@contextlib.contextmanager
def cache_lock(cache, identity):
    root = Path(cache).resolve() / "locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / (identity + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineError(
                "source analysis is already running; do not duplicate it"
            ) from None
        yield


def transcribe_full(audio, duration, cache, model):
    """Ten-minute checkpoints with content hashes; restart keeps completed chunks."""
    from faster_whisper import WhisperModel

    identity = fingerprint(
        {
            "audio": digest(audio),
            "model": model,
            "language": "ja",
            "chunk_seconds": 600,
            "transcription_version": 1,
        }
    )
    root = Path(cache) / "transcripts" / identity
    engine, segments = None, []
    for i, start in enumerate(range(0, int(duration + 0.999), 600)):
        length = min(600, duration - start)
        if length <= 0:
            continue
        key = fingerprint({"identity": identity, "start": start, "length": length})

        def produce(start=start, length=length):
            nonlocal engine
            engine = engine or WhisperModel(
                model, device="cpu", compute_type="int8", cpu_threads=2, num_workers=1
            )
            with tempfile.TemporaryDirectory(prefix=".asr-", dir=root) as temp:
                wav = Path(temp) / "chunk.wav"
                process(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-ss",
                        str(start),
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        audio,
                        "-t",
                        str(length),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        wav,
                    ],
                    timeout=300,
                )
                return transcribe_audio(
                    wav, model, offset=start, engine=engine
                ).model_dump()

        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = stage(root, f"chunk_{i:04}", key, produce)
        transcript = Transcript.model_validate(value)
        if any(
            s.start < start - 0.01 or s.end > start + length + 0.3
            for s in transcript.segments
        ):
            raise PipelineError("ASR checkpoint exceeds source range")
        segments.extend(transcript.segments)
        print(
            json.dumps(
                {
                    "status": "transcribed_chunk",
                    "chunk": i + 1,
                    "source_end": round(start + length, 2),
                }
            ),
            flush=True,
        )
    return Transcript(language="ja", segments=segments)


def cached_transcript(cache, url, model):
    """Read verified complete checkpoints; this path never starts ASR."""
    cache = Path(cache)
    audio = cache / "audio" / (fingerprint({"source": url}) + ".m4a")
    receipt = audio.with_suffix(".json")
    if not audio.is_file() or not receipt.is_file():
        raise PipelineError(
            "reviewed intervals need a complete matching ASR cache",
            code="asr_cache_required",
        )
    info = json.loads(receipt.read_text())
    if info.get("source_url") != url or digest(audio) != info["sha256"]:
        raise PipelineError("reviewed audio cache changed", code="asr_cache_integrity")
    duration = info["source_duration"]
    identity = fingerprint(
        {
            "audio": info["sha256"],
            "model": model,
            "language": "ja",
            "chunk_seconds": 600,
            "transcription_version": 1,
        }
    )
    root = cache / "transcripts" / identity
    segments = []
    for i, start in enumerate(range(0, int(duration + 0.999), 600)):
        length = min(600, duration - start)
        if length <= 0:
            continue
        data, state = (
            root / f"chunk_{i:04}.json",
            root / ".state" / f"chunk_{i:04}.json",
        )
        if not data.is_file() or not state.is_file():
            raise PipelineError(
                "reviewed ASR cache is incomplete", code="asr_cache_required"
            )
        saved = json.loads(state.read_text())
        key = fingerprint({"identity": identity, "start": start, "length": length})
        if saved.get("key") != key or saved.get("sha256") != digest(data):
            raise PipelineError(
                "reviewed ASR cache changed", code="asr_cache_integrity"
            )
        value = Transcript.model_validate_json(data.read_text())
        if any(
            s.start < start - 0.01 or s.end > start + length + 0.3
            for s in value.segments
        ):
            raise PipelineError(
                "reviewed checkpoint exceeds source range", code="asr_cache_integrity"
            )
        segments.extend(value.segments)
    return Transcript(language="ja", segments=segments), info


def automatic(
    url,
    output,
    cache,
    assets,
    output_kind="both",
    count=3,
    clip_seconds=45,
    topic_seconds=300,
    quality="720p",
    model="small",
    style=None,
    topic_style=None,
    resume=False,
    plan_only=False,
    max_bytes=2 * 1024**3,
):
    url = canonical(url)
    if output_kind not in {"shorts", "topics", "both"}:
        raise PipelineError("invalid output kind")
    if (
        not 3 <= count <= 5
        or not 30 <= clip_seconds <= 180
        or not 60 <= topic_seconds <= 1200
    ):
        raise PipelineError("invalid count or target duration")
    output, cache = Path(output).resolve(), Path(cache).resolve()
    if output.exists() and not resume:
        raise PipelineError("output exists; choose another directory or resume")
    identity = fingerprint({"source": url})
    with cache_lock(cache, identity):
        audio = cache / "audio" / (identity + ".m4a")
        receipt = audio.with_suffix(".json")
        used = 0
        if audio.exists():
            if not receipt.is_file():
                raise PipelineError("audio cache receipt is missing")
            info = json.loads(receipt.read_text())
            if digest(audio) != info["sha256"]:
                raise PipelineError("audio cache checksum mismatch")
        else:
            vod = KickVOD(url, cache / "segments", max_bytes)
            try:
                info = vod.retrieve(audio, audio_only=True)
                used = vod.used
                write_json(receipt, info)
            finally:
                vod.close()
        transcript = transcribe_full(audio, info["source_duration"], cache, model)
        kinds = ["shorts", "topics"] if output_kind == "both" else [output_kind]
        manifests = []
        for kind in kinds:
            manifest = plan(
                url,
                info["source_duration"],
                transcript,
                audio,
                count,
                topic_seconds if kind == "topics" else clip_seconds,
                kind,
            )
            selected_style = topic_style if kind == "topics" and topic_style else style
            if selected_style:
                manifest.style = RichStyle.model_validate_json(
                    Path(selected_style).read_text()
                )
            manifests.append(manifest)
        # Finish both plans before spending any high-quality retrieval/render budget.
        if plan_only:
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            for manifest in manifests:
                write_json(
                    output / (manifest.output_kind + "_manifest.json"),
                    manifest.model_dump(),
                )
            return {
                "status": "planned",
                "outputs": kinds,
                "human_review_performed": False,
            }
        results = []
        for manifest in manifests:
            remaining = max_bytes - used
            if remaining <= 0:
                raise PipelineError("shared media budget exhausted")
            destination = (
                output / manifest.output_kind if output_kind == "both" else output
            )
            result = run(
                manifest, destination, cache, assets, quality, model, resume, remaining
            )
            used += result["media_budget_used"]
            results.append(
                {"output_kind": manifest.output_kind, "clips": len(result["clips"])}
            )
        summary = {
            "status": "complete",
            "outputs": results,
            "media_budget_used": used,
            "selection_method": "local_transcript_audio",
            "human_review_performed": False,
            "remote_model_requests": 0,
        }
        if output_kind == "both":
            write_json(output / "production_result.json", summary)
        return summary


def reviewed(
    plan,
    output,
    cache,
    assets,
    quality="720p",
    model="small",
    resume=False,
    max_bytes=2 * 1024**3,
):
    """Render a provided private plan; no global analysis or remote model request."""
    output, cache = Path(output).resolve(), Path(cache).resolve()
    identity = fingerprint({"source": plan.shorts.source_url})
    with cache_lock(cache, identity):
        if output.exists() and not resume:
            raise PipelineError("output exists; choose another directory or resume")
        cache_reused = any(
            not c.subtitles for m in (plan.shorts, plan.topics) for c in m.clips
        )
        if cache_reused:
            from .batch import clipped_subtitles
            from .batch_models import ClipsManifest, ProductionPlan

            transcript, info = cached_transcript(cache, plan.shorts.source_url, model)
            if abs(info["source_duration"] - plan.shorts.source_duration) > 2:
                raise PipelineError(
                    "reviewed plan differs from cached source",
                    code="asr_cache_integrity",
                )
            filled = {}
            for kind in ("shorts", "topics"):
                manifest = getattr(plan, kind)
                clips = []
                for c in manifest.clips:
                    subs = c.subtitles or clipped_subtitles(
                        transcript, c.start_time, c.end_time
                    )
                    if not subs:
                        raise PipelineError(
                            "reviewed interval has no cached speech",
                            code="reviewed_empty_speech",
                        )
                    clips.append(c.model_copy(update={"subtitles": subs}))
                filled[kind] = ClipsManifest.model_validate(
                    {**manifest.model_dump(), "clips": [c.model_dump() for c in clips]}
                )
            plan = ProductionPlan.model_validate({**plan.model_dump(), **filled})
        used, rows = 0, []
        for manifest in (plan.shorts, plan.topics):
            if used >= max_bytes:
                raise PipelineError("shared media budget exhausted")
            result = run(
                manifest,
                output / manifest.output_kind,
                cache,
                assets,
                quality,
                model,
                resume,
                max_bytes - used,
            )
            used += result["media_budget_used"]
            rows.append(
                {"output_kind": manifest.output_kind, "clips": len(result["clips"])}
            )
        write_json(output / "production_plan.json", plan.model_dump())
        summary = {
            "status": "complete",
            "outputs": rows,
            "media_budget_used": used,
            "selection_method": "manual",
            "review_basis": plan.review_basis,
            "human_review_performed": False,
            "remote_model_requests": 0,
            "asr_cache_reused": cache_reused,
            "global_asr_performed": False,
            "asr_model": model if cache_reused else None,
        }
        write_json(output / "production_result.json", summary)
        return summary
