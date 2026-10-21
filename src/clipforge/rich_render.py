"""CPU rich renderer with local assets and speech-first audio mixing."""

import re
from pathlib import Path

from . import captions, media
from .batch_models import RichStyle
from .models import SelectedClip, Settings, Transcript
from .storage import PipelineError, digest, write_json


def asset(root, relative, extensions):
    if relative is None:
        return None
    base = Path(root).resolve()
    path = base / relative
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        raise PipelineError("asset escapes asset root")
    path = path.resolve(strict=True)
    if (
        not path.is_file()
        or path.suffix.lower() not in extensions
        or path.stat().st_size > 32 * 1024**2
    ):
        raise PipelineError("unsupported or oversized asset")
    return path


def color(value):
    import pysubs2

    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise PipelineError("colors must use #RRGGBB")
    return pysubs2.Color(*(int(value[i : i + 2], 16) for i in (1, 3, 5)))


def font_directory(assets, relative):
    if not relative:
        return None
    path = (Path(assets).resolve() / relative).resolve(strict=True)
    if (
        not path.is_relative_to(Path(assets).resolve())
        or not path.is_dir()
        or any(c in str(path) for c in "':,[];\\")
    ):
        raise PipelineError("invalid font directory")
    return path


def render(source, directory, clip, style: RichStyle, assets, output_kind="shorts"):
    import pysubs2

    source, directory = Path(source).resolve(), Path(directory).resolve()
    if directory.exists():
        raise PipelineError("rich output directory exists")
    if source.is_relative_to(directory):
        raise PipelineError("source must be outside output directory")
    directory.mkdir(parents=True, mode=0o700)
    info = media.probe(source)
    duration = clip.end_time - clip.start_time
    if abs(info["duration"] - duration) > 0.3:
        raise PipelineError("clip source duration does not match original interval")
    settings = Settings(
        gemini_model="local-batch",
        height=style.height,
        font=style.font,
        framing="blur-fit",
        headline=True,
        hook_title=clip.hook_title,
        aspect="16:9" if output_kind == "topics" else "9:16",
    )
    local = SelectedClip(
        start=0,
        end=duration,
        candidate_id=clip.clip_id,
        title=clip.hook_title,
        reason=clip.reason,
        confidence=clip.score,
    )
    segments = []
    for segment in clip.subtitles:
        value = segment.model_dump()
        value["start"] -= clip.start_time
        value["end"] -= clip.start_time
        for w in value["words"]:
            w["start"] -= clip.start_time
            w["end"] -= clip.start_time
        segments.append(value)
    transcript = Transcript.model_validate({"language": "ja", "segments": segments})
    font_path = font_directory(assets, style.fonts_dir)
    caption_report = captions.create(transcript, local, directory, settings, font_path)
    ass = pysubs2.SSAFile.load(str(directory / "captions.ass"))
    ass.styles["Default"].primarycolor = color(style.caption_color)
    ass.styles["Default"].outlinecolor = color(style.outline_color)
    ass.styles["Default"].secondarycolor = color(style.highlight_color)
    ass.styles["Headline"].borderstyle = 3
    ass.styles["Headline"].outline = 12
    ass.styles["Headline"].outlinecolor = pysubs2.Color(0, 0, 0, 35)
    ass.styles["Headline"].backcolor = pysubs2.Color(0, 0, 0, 35)
    for event in ass:
        if event.style == "Default":
            event.text = r"{\fad(60,60)}" + event.text
        elif event.style == "Headline" and output_kind == "topics":
            event.end = min(event.end, 8000)
    ass.save(str(directory / "captions.ass"))
    graph, width, height = media.layout(settings, info)
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-n",
        *media.PROTOCOLS,
        *media.FORMATS,
        "-i",
        str(source),
    ]
    audio, index, hashes = [], 1, {}
    bgm = asset(assets, style.bgm, {".wav", ".mp3", ".m4a", ".ogg"})
    se = asset(assets, style.se, {".wav", ".mp3", ".m4a", ".ogg"})
    overlay = asset(assets, style.overlay, {".png"})
    for path, gain, loop in [(bgm, style.bgm_gain, True), (se, style.se_gain, False)]:
        if path:
            command += (["-stream_loop", "-1"] if loop else []) + [
                *media.PROTOCOLS,
                "-i",
                str(path),
            ]
            audio.append((index, gain, loop))
            hashes[str(path.relative_to(Path(assets).resolve()))] = digest(path)
            index += 1
    graph += "[canvas];"
    if overlay:
        command += ["-loop", "1", *media.PROTOCOLS, "-i", str(overlay)]
        graph += f"[{index}:v]scale={width}:{height}[frame];[canvas][frame]overlay=0:0:shortest=1[framed];"
        video_input = "framed"
        hashes[str(overlay.relative_to(Path(assets).resolve()))] = digest(overlay)
    else:
        video_input = "canvas"
    fonts = ""
    if style.fonts_dir:
        path = (Path(assets).resolve() / style.fonts_dir).resolve(strict=True)
        if (
            not path.is_relative_to(Path(assets).resolve())
            or not path.is_dir()
            or any(c in str(path) for c in "':,[];\\")
        ):
            raise PipelineError("invalid font directory")
        fonts = ":fontsdir='" + str(path) + "'"
        hashes.update(
            {
                str(p.relative_to(Path(assets).resolve())): digest(p)
                for p in path.iterdir()
                if p.suffix.lower() in {".ttf", ".otf", ".ttc"} and not p.is_symlink()
            }
        )
    graph += f"[{video_input}]ass=filename=captions.ass{fonts}[v];"
    if info["audio"]:
        graph += (
            "[0:a]asetpts=PTS-STARTPTS,aresample=48000,apad,atrim=duration="
            + str(duration)
            + "[speech];"
        )
    else:
        graph += f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}[speech];"
    if bgm and info["audio"]:
        graph += "[speech]asplit=2[speech_mix][duckkey];"
        inputs = ["[speech_mix]"]
    else:
        inputs = ["[speech]"]
    for i, gain, loop in audio:
        graph += f"[{i}:a]asetpts=PTS-STARTPTS,aresample=48000,volume={gain},apad,atrim=duration={duration}"
        if loop:
            graph += (
                ",afade=t=in:d=0.5,afade=t=out:st=" + str(max(0, duration - 1)) + ":d=1"
            )
        graph += f"[asset{i}];"
        if loop and info["audio"]:
            graph += f"[asset{i}][duckkey]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=300[ducked{i}];"
            inputs.append(f"[ducked{i}]")
        else:
            inputs.append(f"[asset{i}]")
    graph += (
        "".join(inputs)
        + f"amix=inputs={len(inputs)}:duration=first:normalize=0,loudnorm=I=-16:TP=-2:LRA=11,"
        "alimiter=limit=0.794328:level=false:latency=true[a]"
    )
    destination = directory / "video.mp4"
    command += [
        "-filter_complex_threads",
        "2",
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        str(duration),
        "-r",
        info["fps"],
        "-c:v",
        "libx264",
        "-preset",
        style.preset,
        "-crf",
        str(style.crf),
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "2",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    # All paths in the graph are fixed basenames or validated local asset paths.
    import subprocess

    result = subprocess.run(
        command, cwd=directory, capture_output=True, timeout=max(300, duration * 25)
    )
    if result.returncode:
        raise PipelineError("rich rendering failed", code="render_ffmpeg")
    output_info = media.probe(destination)
    if abs(output_info["duration"] - duration) > 0.3 or (
        output_info["width"],
        output_info["height"],
    ) != (width, height):
        raise PipelineError("rich output dimensions or duration mismatch")
    receipt = {
        "status": "rendered",
        "clip": clip.model_dump(),
        "media": output_info,
        "source_sha256": digest(source),
        "assets_sha256": hashes,
        "output_sha256": digest(destination),
        "subtitle_events": len(clip.subtitles),
        "caption_events": caption_report["events"],
        "caption_review": caption_report,
        "output_kind": output_kind,
        "style": style.model_dump(),
    }
    from .quality import inspect_video

    receipt["quality"] = inspect_video(destination, duration, width, height)
    write_json(directory / "render.json", receipt)
    return receipt
