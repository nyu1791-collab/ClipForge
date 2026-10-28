"""Full-video and selected-range translation pipeline."""

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tempfile
import time
import uuid

from clipforge.media_sources import open_source
from clipforge.gap_diagnostics import describe_gaps
from clipforge.storage import PipelineError, digest, process, write_json
from clipforge.transcribe_adapter import word_segments
from .backend import Vertex, VertexHTTPError, identity
from .captions import apply_translation, make_cues, write_subtitles

WINDOW = 898.0
OVERLAP = 2.0


def run_command(command, timeout=1800, cwd=None):
    try:
        return process(command, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        raise PipelineError(
            f"Required executable not found: {Path(str(command[0])).name}"
        ) from None


def probe(path):
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            path,
        ]
    )
    info = json.loads(result.stdout)
    duration = float(info["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise PipelineError("Invalid media duration")
    streams = {s.get("codec_type") for s in info.get("streams", [])}
    if "video" not in streams or "audio" not in streams:
        raise PipelineError("Media requires video and audio streams")
    return duration


def acquire(source, output, max_bytes, max_seconds):
    if not source.startswith("https://"):
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise PipelineError("Local input file was not found")
        return path, digest(path)
    video = output / "source.mp4"
    stamp = output / "source-identity.json"
    source_key = hashlib.sha256(source.encode()).hexdigest()
    if video.exists() and stamp.exists():
        record = json.loads(stamp.read_text())
        source_hash = digest(video)
        if (
            record.get("source_key") != source_key
            or record.get("sha256") != source_hash
        ):
            raise PipelineError("Saved source does not match this URL")
        return video, source_hash
    if video.exists() or stamp.exists():
        raise PipelineError("Incomplete saved source; use a fresh output directory")
    vod = open_source(
        source, output / "source-cache", max_bytes=max_bytes, max_seconds=max_seconds
    )
    try:
        vod.retrieve(video, start=0.0, end=None, quality="360p")
    except Exception:
        video.unlink(missing_ok=True)
        raise
    finally:
        vod.close()
    source_hash = digest(video)
    write_json(stamp, {"source_key": source_key, "sha256": source_hash})
    return video, source_hash


def windows(duration):
    starts = [0.0]
    while starts[-1] + WINDOW < duration:
        starts.append(starts[-1] + WINDOW - OVERLAP)
    return starts


def encode_audio(video, path, start, duration):
    run_command(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-ss",
            str(start),
            "-i",
            str(video),
            "-t",
            str(duration),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            "-application",
            "voip",
            "-compression_level",
            "3",
            str(path),
        ],
        timeout=max(300, int(duration * 2)),
    )


def process_window(
    client,
    video,
    source_hash,
    begin,
    duration,
    whole_duration,
    clip_start,
    chunk,
    total,
    options,
):
    recovery = []
    with tempfile.TemporaryDirectory(prefix="translation-audio-") as temp:
        audio = Path(temp) / "audio.ogg"
        encode_audio(video, audio, clip_start + begin, duration)
        raw = client.transcribe(
            audio,
            options["source_language"],
            {
                "source_sha256": source_hash,
                "clip_start": clip_start,
                "offset": begin,
                "duration": duration,
                "encoding": "ogg-opus-16k-mono-24k-v1",
            },
        )
        local_words, untimed = word_segments(raw, 0, duration)
        words = [
            {**w, "start": w["start"] + begin, "end": w["end"] + begin}
            for w in local_words
        ]
        if options["gap_recovery"]:
            levels = describe_gaps(audio, duration, local_words)
            candidates = [
                g
                for g in levels.get("gaps", [])
                if g["end"] - g["start"] >= 15
                and g.get("peak_one_second_rms_dbfs", -200) >= -48
            ]
            candidates.sort(key=lambda g: -(g["end"] - g["start"]))
            for i, gap in enumerate(candidates[:2]):
                core_start, core_end = gap["start"], min(gap["end"], gap["start"] + 300)
                request_start, request_end = (
                    max(0, core_start - 2),
                    min(duration, core_end + 2),
                )
                recovered_audio = Path(temp) / f"gap-{i}.ogg"
                try:
                    encode_audio(
                        video,
                        recovered_audio,
                        clip_start + begin + request_start,
                        request_end - request_start,
                    )
                    result = client.transcribe(
                        recovered_audio,
                        options["source_language"],
                        {
                            "source_sha256": source_hash,
                            "clip_start": clip_start,
                            "offset": begin + request_start,
                            "duration": request_end - request_start,
                            "encoding": "ogg-opus-16k-mono-24k-v1",
                        },
                    )
                    extra, no_time = word_segments(
                        result, begin + request_start, whole_duration
                    )
                    words.extend(
                        {
                            **w,
                            "source_speaker": f"recovery-{chunk}-{i}:{w.get('source_speaker')}",
                        }
                        for w in extra
                        if begin + core_start
                        <= (w["start"] + w["end"]) / 2
                        < begin + core_end
                    )
                    untimed.extend(no_time)
                    recovery.append(
                        {
                            "start": begin + core_start,
                            "end": begin + core_end,
                            "status": "received",
                        }
                    )
                except VertexHTTPError as error:
                    if error.status in (401, 403):
                        raise
                    recovery.append(
                        {
                            "start": begin + core_start,
                            "end": begin + core_end,
                            "status": "unavailable",
                        }
                    )
                except PipelineError:
                    recovery.append(
                        {
                            "start": begin + core_start,
                            "end": begin + core_end,
                            "status": "unavailable",
                        }
                    )
    left = begin if chunk == 0 else begin + OVERLAP / 2
    right = min(
        whole_duration, begin + duration - (OVERLAP / 2 if chunk + 1 < total else 0)
    )
    words = [w for w in words if left <= (w["start"] + w["end"]) / 2 < right]
    cues = make_cues(words, chunk)
    translated, missing = [], []
    for pos in range(0, len(cues), options["batch_size"]):
        batch = cues[pos : pos + options["batch_size"]]
        response = client.translate(
            batch, options["target_language"], options["model"], options["thinking"]
        )
        rows, absent = apply_translation(batch, response)
        translated.extend(rows)
        missing.extend(absent)
    return {
        "chunk": chunk,
        "cues": translated,
        "missing": missing,
        "untimed": untimed,
        "gap_recovery": recovery,
    }


def render(video, output, start, duration, height, encoder, preset):
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-ss",
        str(start),
        "-i",
        str(video),
        "-t",
        str(duration),
        "-vf",
    ]
    vf = (f"scale=-2:{height}," if height else "") + "ass=subtitles.ass"
    cmd += [vf, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", encoder]
    if encoder == "libx264":
        cmd += ["-preset", preset, "-crf", "23"]
    else:
        cmd += ["-preset", "p4", "-cq", "25"]
    temporary = output / (".video-" + uuid.uuid4().hex + ".mp4")
    cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary)]
    try:
        run_command(cmd, timeout=max(600, int(duration * 5)), cwd=output)
        temporary.replace(output / "video.mp4")
    finally:
        temporary.unlink(missing_ok=True)


def run(source, output, options):
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    start_time = time.monotonic()
    video, source_hash = acquire(
        source, output, options["max_bytes"], options["max_seconds"]
    )
    full_duration = probe(video)
    clip_start = options["start"]
    clip_end = options["end"] if options["end"] is not None else full_duration
    if not (0 <= clip_start < clip_end <= full_duration + 0.01):
        raise PipelineError("Selected time range is outside the source video")
    clip_end = min(clip_end, full_duration)
    duration = clip_end - clip_start
    if duration > options["max_seconds"]:
        raise PipelineError("Source exceeds the configured duration limit")
    run_key = identity(
        {
            "source_sha256": source_hash,
            "start": clip_start,
            "end": clip_end,
            **{
                k: v
                for k, v in options.items()
                if k not in {"max_bytes", "max_seconds", "workers"}
            },
        }
    )
    manifest_path = output / "manifest.json"
    state_path = output / "run-state.json"
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text())
        if prior.get("run_key") != run_key:
            raise PipelineError(
                "Output directory belongs to different settings or media"
            )
        files = [output / name for name in prior.get("files", [])]
        if (
            prior.get("status") in {"complete", "partial", "no_timed_speech"}
            and files
            and all(f.is_file() for f in files)
        ):
            return prior
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("run_key") != run_key:
            raise PipelineError("Saved run belongs to different settings or media")
    elif (output / "video.mp4").exists():
        raise PipelineError(
            "Unidentified output video exists; use a fresh output directory"
        )
    else:
        write_json(state_path, {"run_key": run_key})
    client = Vertex(options["project"], output / "cache")
    starts = windows(duration)
    results = []
    with ThreadPoolExecutor(max_workers=min(options["workers"], len(starts))) as pool:
        pending = [
            pool.submit(
                process_window,
                client,
                video,
                source_hash,
                begin,
                min(WINDOW, duration - begin),
                duration,
                clip_start,
                i,
                len(starts),
                options,
            )
            for i, begin in enumerate(starts)
        ]
        for future in as_completed(pending):
            results.append(future.result())
    results.sort(key=lambda r: r["chunk"])
    cues = sorted(
        [c for result in results for c in result["cues"]],
        key=lambda r: (r["start"], r["end"]),
    )
    missing = [item for result in results for item in result["missing"]]
    untimed = [
        {"chunk": r["chunk"], "text": text} for r in results for text in r["untimed"]
    ]
    write_json(
        output / "translation.json",
        {
            "cues": cues,
            "untranslated_ids": missing,
            "untimed_source": untimed,
            "gap_recovery": [g for r in results for g in r["gap_recovery"]],
        },
    )
    write_subtitles(
        cues,
        output,
        bilingual=options["bilingual"],
        font=options["font"],
        size=options["font_size"],
        line_chars=options["line_chars"],
    )
    files = ["translation.json", "subtitles.ass", "subtitles.srt"]
    if not options["subtitles_only"]:
        if not (output / "video.mp4").exists():
            render(
                video,
                output,
                clip_start,
                duration,
                options["height"],
                options["encoder"],
                options["preset"],
            )
        files.append("video.mp4")
    manifest = {
        "status": "no_timed_speech"
        if not cues
        else "partial"
        if missing or untimed
        else "complete",
        "run_key": run_key,
        "source_sha256": source_hash,
        "start_seconds": clip_start,
        "end_seconds": clip_end,
        "duration_seconds": duration,
        "source_language": options["source_language"] or "auto",
        "target_language": options["target_language"],
        "transcription_model": "gemini-3.5-transcribe",
        "translation_model": options["model"],
        "thinking": options["thinking"],
        "chunks": len(starts),
        "cue_count": len(cues),
        "untranslated_count": len(missing),
        "untimed_count": len(untimed),
        "subtitle_timing": "source utterance offsets; not translated word alignment",
        "files": files,
        "elapsed_seconds": round(time.monotonic() - start_time, 2),
    }
    write_json(manifest_path, manifest)
    return manifest
