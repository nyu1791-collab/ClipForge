"""Local transcript/audio planning and transactional multi-clip rendering."""

import json
import math
import tempfile
from pathlib import Path

from .batch_models import BatchClip, ClipsManifest
from .kick_media import KickVOD
from .models import Segment, Transcript
from .rich_render import asset, font_directory, render
from .storage import PipelineError, digest, fingerprint, process, stage, write_json


def transcribe_audio(path, model="small", language="ja", offset=0, engine=None):
    from faster_whisper import WhisperModel

    engine = engine or WhisperModel(
        model, device="cpu", compute_type="int8", cpu_threads=2, num_workers=1
    )
    stream, info = engine.transcribe(
        str(path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    segments = []
    for s in stream:
        if s.end <= s.start or not s.text.strip():
            continue
        segments.append(
            Segment.model_validate(
                {
                    "start": max(0, s.start) + offset,
                    "end": s.end + offset,
                    "text": s.text,
                    "words": [
                        {
                            "start": max(s.start, w.start) + offset,
                            "end": min(s.end, w.end) + offset,
                            "text": w.word,
                        }
                        for w in s.words or []
                        if w.end > w.start and w.word.strip()
                    ],
                }
            )
        )
    return Transcript(language=info.language, segments=segments)


def clipped_subtitles(transcript, start, end):
    result = []
    for s in transcript.segments:
        if s.end <= start or s.start >= end:
            continue
        value = s.model_dump()
        value.update(start=max(start, s.start), end=min(end, s.end))
        value["words"] = [
            {**w.model_dump(), "start": max(start, w.start), "end": min(end, w.end)}
            for w in s.words
            if w.end > start and w.start < end
        ]
        result.append(Segment.model_validate(value))
    return result


def grams(text):
    text = "".join(c.lower() for c in text if c.isalnum())
    return {text[i : i + 3] for i in range(max(0, len(text) - 2))}


def speech_end(transcript, start, seconds, duration, minimum, maximum):
    target = min(duration, start + seconds)
    ends = [
        s.end
        for s in transcript.segments
        if start + minimum <= s.end <= min(duration, start + maximum)
        and abs(s.end - target) <= max(8, seconds * 0.25)
    ]
    # Only use actual utterance ends. Refuse a candidate rather than cutting speech.
    return min(ends, key=lambda v: abs(v - target)) if ends else start


def extract_title(subtitles):
    """An extractive label, shortened at a word boundary, never an invented hook."""
    from .caption_layout import normalize, units

    text = "".join(s.text for s in subtitles).strip()
    parts, title = units(normalize(text[:200])), ""
    for part in parts:
        if len(title + part) > 28:
            break
        title += part
        if title.rstrip().endswith(("。", "！", "？")) and len(title) >= 8:
            break
    return title.strip() or "話題候補"


def plan(url, duration, transcript, audio, count=3, seconds=45, output_kind="shorts"):
    """Extractive topic boundaries and RMS peaks; no unrequested remote model."""
    if output_kind == "topics":
        from .topic_selection import plan_topics

        return plan_topics(url, duration, transcript, count, seconds)
    if not 3 <= count <= 5 or not 30 <= seconds <= 180:
        raise PipelineError("planner requires 3-5 clips of 30-180 seconds")
    if not transcript.segments:
        raise PipelineError("no speech found; cannot invent topic clips")
    pool = []
    previous = ""
    for index, s in enumerate(transcript.segments):
        if s.start + 30 > duration:
            continue
        start = s.start
        stop = speech_end(transcript, start, seconds, duration, 30, 180)
        text = "".join(
            t.text for t in transcript.segments if t.start < stop and t.end > start
        )
        left, right = grams(previous), grams(text)
        novelty = 1 - len(left & right) / max(1, len(left | right))
        gap = s.start - transcript.segments[index - 1].end if index else 0
        score = min(
            0.95,
            0.35 + min(0.25, len(right) / 500) + novelty * 0.2 + min(0.15, gap / 10),
        )
        if stop - start < 30:
            continue
        pool.append(
            {
                "start": start,
                "end": stop,
                "type": "topic",
                "score": score,
                "reason": "発話の区切り・語彙の変化・発話密度で抽出した話題候補。意味的完結は要確認。",
            }
        )
        previous = text
    # One-second RMS peaks supplement linguistic candidates with local audio evidence.
    import array
    import wave

    with tempfile.TemporaryDirectory() as temp:
        wav = Path(temp) / "rms.wav"
        process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                audio,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-c:a",
                "pcm_s16le",
                wav,
            ],
            timeout=max(300, duration),
        )
        rms = []
        with wave.open(str(wav), "rb") as source:
            while chunk := source.readframes(8000):
                samples = array.array("h", chunk)
                rms.append(
                    math.sqrt(sum(float(x) ** 2 for x in samples) / len(samples))
                )
    median = sorted(rms)[len(rms) // 2] if rms else 1
    for t in sorted(range(len(rms)), key=lambda t: rms[t], reverse=True)[:100]:
        start = min(max(0, t - seconds * 0.4), duration - seconds)
        if start < 0 or not any(
            s.start < start + seconds and s.end > start for s in transcript.segments
        ):
            continue
        starts = [s.start for s in transcript.segments if abs(s.start - start) <= 5]
        if not starts:
            continue
        start = min(starts, key=lambda v: abs(v - start))
        stop = speech_end(transcript, start, seconds, duration, 30, 180)
        if stop - start < 30 or not start <= t <= stop:
            continue
        pool.append(
            {
                "start": start,
                "end": stop,
                "type": "highlight",
                "score": min(0.98, 0.5 + 0.15 * rms[t] / max(1, median)),
                "reason": f"音声RMSが全体中央値の{rms[t] / max(1, median):.2f}倍。大声・物音の可能性があり、人による確認が必要。",
            }
        )
    chosen, seen = [], []
    for desired in ["topic", "highlight"] + [None] * count:
        if len(chosen) == count:
            break
        for candidate in sorted(pool, key=lambda c: c["score"], reverse=True):
            if desired and candidate["type"] != desired:
                continue
            if any(
                max(
                    0,
                    min(candidate["end"], c["end"])
                    - max(candidate["start"], c["start"]),
                )
                > 0.1
                for c in chosen
            ):
                continue
            subs = clipped_subtitles(transcript, candidate["start"], candidate["end"])
            text = "".join(s.text for s in subs).strip()
            tokens = grams(text)
            if any(len(tokens & g) / max(1, len(tokens | g)) > 0.65 for g in seen):
                continue
            candidate["subtitles"] = subs
            candidate["hook_title"] = extract_title(subs)
            chosen.append(candidate)
            seen.append(tokens)
            break
    if len(chosen) < count:
        raise PipelineError(
            "insufficient distinct candidates; review transcript instead of duplicating clips"
        )
    clips = [
        BatchClip(
            clip_id=f"clip_{i:02}",
            type=c["type"],
            start_time=c["start"],
            end_time=c["end"],
            subtitles=c["subtitles"],
            score=c["score"],
            reason=c["reason"],
            hook_title=c["hook_title"],
        )
        for i, c in enumerate(sorted(chosen, key=lambda c: c["start"]), 1)
    ]
    return ClipsManifest(
        source_url=url,
        source_duration=duration,
        clips=clips,
        selection_method="local_transcript_audio",
    )


def run(
    manifest,
    output,
    cache,
    assets,
    quality="720p",
    whisper_model="small",
    resume=False,
    max_bytes=2 * 1024**3,
):
    output, cache = Path(output).resolve(), Path(cache).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(p.is_symlink() for p in output.rglob("*")):
        raise PipelineError("output symlinks unsupported")
    import fcntl

    lock = (output / ".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise PipelineError("batch output is in use") from None
    vod = None
    try:
        hashes = {}
        for field, suffixes in [
            ("bgm", {".wav", ".m4a", ".mp3", ".ogg"}),
            ("se", {".wav", ".m4a", ".mp3", ".ogg"}),
            ("overlay", {".png"}),
        ]:
            relative = getattr(manifest.style, field)
            path = asset(assets, relative, suffixes)
            if path:
                hashes[relative] = digest(path)
        if manifest.style.fonts_dir:
            fonts = (Path(assets).resolve() / manifest.style.fonts_dir).resolve(
                strict=True
            )
            if not fonts.is_relative_to(Path(assets).resolve()):
                raise PipelineError("font path escapes root")
            hashes.update(
                {
                    str(p.relative_to(Path(assets).resolve())): digest(p)
                    for p in fonts.iterdir()
                    if p.suffix.lower() in {".otf", ".ttf", ".ttc"}
                    and not p.is_symlink()
                }
            )
        implementation = fingerprint(
            {p.name: digest(p) for p in Path(__file__).parent.glob("*.py")}
        )
        from importlib.metadata import version

        from .fonts import Measure

        runtime = {
            "packages": {
                name: version(name)
                for name in ("faster-whisper", "pysubs2", "Janome", "budoux", "pillow")
            },
            "font_sha256": digest(
                Measure(
                    manifest.style.font,
                    35,
                    font_directory(assets, manifest.style.fonts_dir),
                ).path
            ),
            "ffmpeg": process(["ffmpeg", "-version"]).stdout.decode().splitlines()[0],
        }
        key = fingerprint(
            {
                "manifest": manifest.model_dump(),
                "assets": hashes,
                "quality": quality,
                "whisper_model": whisper_model,
                "implementation": implementation,
                "runtime": runtime,
            }
        )
        old = output / "batch_input.json"
        if old.exists():
            if not resume or json.loads(old.read_text()).get("key") != key:
                raise PipelineError("output exists or resume inputs differ")
        elif any(p.name != ".lock" for p in output.iterdir()):
            raise PipelineError("output is not empty")
        write_json(
            old,
            {"key": key, "manifest": manifest.model_dump(), "assets_sha256": hashes},
        )
        vod = KickVOD(
            manifest.source_url,
            cache / "segments",
            max_bytes,
            max(14400, manifest.source_duration + 60),
        )
        results, filled = [], []
        for clip in manifest.clips:
            target = output / clip.clip_id
            if target.exists():
                receipt = json.loads((target / "render.json").read_text())
                if receipt.get("batch_key") != key or any(
                    digest(target / name) != sha
                    for name, sha in receipt["files_sha256"].items()
                ):
                    raise PipelineError(
                        "rendered clip changed; use a new output directory"
                    )
                filled.append(BatchClip.model_validate(receipt["clip"]))
                results.append(receipt)
                continue
            print(f"Fetching {clip.clip_id} at {quality}…", flush=True)
            media_key = fingerprint(
                {
                    "url": manifest.source_url,
                    "start": clip.start_time,
                    "end": clip.end_time,
                    "quality": quality,
                }
            )
            source = cache / "ranges" / (media_key + ".mp4")
            fetched = source.with_suffix(".json")
            if source.exists():
                if not fetched.exists() or json.loads(fetched.read_text())[
                    "sha256"
                ] != digest(source):
                    raise PipelineError("range cache has no matching receipt")
                download = json.loads(fetched.read_text())
            else:
                download = vod.retrieve(source, clip.start_time, clip.end_time, quality)
                write_json(fetched, download)
            if abs(download["source_duration"] - manifest.source_duration) > 2:
                raise PipelineError("manifest duration differs from archive")
            asr = not clip.subtitles
            if asr:
                print(f"Transcribing {clip.clip_id} on CPU…", flush=True)
                asr_key = fingerprint(
                    {
                        "source": digest(source),
                        "model": whisper_model,
                        "offset": clip.start_time,
                        "asr_version": 1,
                    }
                )
                transcript = Transcript.model_validate(
                    stage(
                        cache / "asr",
                        asr_key,
                        asr_key,
                        lambda: transcribe_audio(
                            source, whisper_model, offset=clip.start_time
                        ).model_dump(),
                    )
                )
                clip = clip.model_copy(
                    update={
                        "subtitles": clipped_subtitles(
                            transcript, clip.start_time, clip.end_time
                        )
                    }
                )
                clip = BatchClip.model_validate(clip.model_dump())
            with tempfile.TemporaryDirectory(
                prefix="." + clip.clip_id, dir=output
            ) as temp:
                staging = Path(temp) / "render"
                receipt = render(
                    source, staging, clip, manifest.style, assets, manifest.output_kind
                )
                receipt.update(
                    batch_key=key,
                    fetch=download,
                    subtitle_source="cpu_asr" if asr else "manifest",
                    whisper_model=whisper_model if asr else None,
                    files_sha256={
                        name: digest(staging / name)
                        for name in (
                            "video.mp4",
                            "captions.ass",
                            "captions.srt",
                            "caption_review.json",
                        )
                    },
                )
                write_json(staging / "render.json", receipt)
                staging.rename(target)
            results.append(receipt)
            filled.append(clip)
            write_json(
                output / "progress.json",
                {
                    "status": "rendering",
                    "completed": len(results),
                    "total": len(manifest.clips),
                },
            )
        manifest = manifest.model_copy(update={"clips": filled})
        write_json(output / "clips_manifest.json", manifest.model_dump())
        result = {
            "status": "complete",
            "key": key,
            "selection_method": manifest.selection_method,
            "output_kind": manifest.output_kind,
            "clips": results,
            "human_review_performed": False,
            "remote_model_requests": 0,
            "media_budget_used": vod.used,
        }
        write_json(output / "batch_result.json", result)
        write_json(
            output / "progress.json",
            {"status": "complete", "completed": len(results), "total": len(results)},
        )
        (output / "failure.json").unlink(missing_ok=True)
        return result
    except Exception as e:
        write_json(
            output / "failure.json",
            {"status": "failed", "error_type": type(e).__name__},
        )
        raise
    finally:
        if vod:
            vod.close()
        lock.close()
