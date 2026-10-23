"""Local, self-contained media only. No shell interpolation or input overwrites."""

import json
import math
from fractions import Fraction
from pathlib import Path
import re
import shutil
import subprocess

from .hardware import encode_threads, video_options
from .storage import TIME_TOLERANCE, PipelineError, process

PROTOCOLS = ["-protocol_whitelist", "file,pipe"]
FORMATS = ["-format_whitelist", "mov,matroska,avi,flv,mpeg,mpegts,ogg,asf,wav,flac,mp3"]
AUDIO_RATE = 48000
ASR_SAMPLE_RATE = 16000
# loudnorm target. TP sits 0.5 dB below the -1.5 dBTP the loudnorm pass itself aims for
# because AAC encoding adds about +0.6 dB of overshoot (3 delivered clips measured -0.9 dBTP).
LOUDNESS = "I=-16:TP=-2.0:LRA=11"
# Sample-peak limit after loudnorm: linear 0.7079, about -3 dBFS. AAC adds up to about
# +1.2 dB on a sharp transient (2026-10-23: -2 dBFS gave -0.8 dBTP on one clip); -3 dBFS
# keeps the decoded true peak under -1 dBTP and moved integrated loudness by 0.2 LU at most.
PEAK_LIMIT = 0.7079
MAX_KEEP_REGIONS = 60
MAX_SOUND_EFFECTS = 20
SPLICE_FADE = 0.015  # seconds at each end of a kept piece of audio
VIDEO_CRF = (
    18  # x264 quality: lower is sharper and larger; 18 is near visually lossless
)
PROGRESS_RGB = (255, 196, 0)
# Chat column background and the thin divider between the video area and the column.
CHAT_BACKGROUND = "0x111114"
CHAT_BORDER = "0x2a2a30"
STANDARD_FRAME_RATES = tuple(
    Fraction(rate)
    for rate in (
        "24000/1001",
        "24",
        "25",
        "30000/1001",
        "30",
        "48",
        "50",
        "60000/1001",
        "60",
    )
)


def final_video_options(settings):
    """Delivery encode: NVENC on a usable GPU, else x264 at the configured preset and CRF 18."""
    options = video_options(settings, VIDEO_CRF)
    if "h264_nvenc" in options:
        return options
    return [
        "-c:v",
        "libx264",
        "-preset",
        settings.encode_preset,
        "-crf",
        str(VIDEO_CRF),
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(encode_threads(settings)),
    ]


def require_tools(captions=True):
    for name in ["ffmpeg", "ffprobe"]:
        if not shutil.which(name):
            raise PipelineError(name + " is not installed")
    if captions:
        output = process(
            ["ffmpeg", "-hide_banner", "-filters"], stage="filters"
        ).stdout.decode(errors="replace")
        if not any(" ass " in line for line in output.splitlines()):
            raise PipelineError("FFmpeg must include the libass ass filter")


def probe(path):
    result = process(
        [
            "ffprobe",
            "-v",
            "error",
            *PROTOCOLS,
            *FORMATS,
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            Path(path).resolve(),
        ],
        stage="probe",
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if not video:
        raise PipelineError("input must contain a video stream")
    duration = float(data.get("format", {}).get("duration", video.get("duration", 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise PipelineError("video duration is unavailable")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "audio": audio is not None,
        # Speech extraction starts at the audio stream's own start, not at 0 s.
        "audio_start": float(audio.get("start_time") or 0) if audio else 0.0,
        "frame_rate": _frame_rate(
            video.get("avg_frame_rate"), video.get("r_frame_rate")
        ),
        "audio_sample_rate": int(audio["sample_rate"])
        if audio and audio.get("sample_rate")
        else None,
    }


def _frame_rate(*values):
    """Prefer exact standard rates, then snap the first usable rate within 0.5%."""
    rates = []
    for value in values:
        try:
            rate = Fraction(value)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            rates.append(rate)
    if not rates:
        return None
    for rate in rates:
        if rate in STANDARD_FRAME_RATES:
            return str(rate)
    rate = rates[0]
    nearest = min(STANDARD_FRAME_RATES, key=lambda standard: abs(rate - standard))
    if abs(rate - nearest) / nearest <= Fraction(1, 200):
        rate = nearest
    return str(rate)


def output_rate(source_rate):
    """Keep the source frame rate up to 30 fps; faster sources are reduced to 30.

    Changing a 25 fps source to 30 fps repeats a frame every fifth frame, which shows as
    judder, so the rate is only ever lowered.
    """
    rate = Fraction(source_rate) if source_rate else Fraction(30)
    return min(rate, Fraction(30))


def audio(source, destination, duration):
    result = process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-n",
            *PROTOCOLS,
            *FORMATS,
            "-i",
            source,
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(ASR_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            destination,
        ],
        timeout=max(180, duration * 2),
        stage="audio",
    )
    if result.stderr.strip():
        raise PipelineError("audio extraction reported a decoding error")


def preview(source, destination, candidate, settings):
    if destination.exists():
        destination.unlink()  # Derived previews only, never the input.
    result = process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-n",
            "-ss",
            f"{candidate.start:.6f}",
            "-threads",
            str(encode_threads(settings)),
            "-filter_threads",
            str(encode_threads(settings)),
            *PROTOCOLS,
            *FORMATS,
            "-i",
            source,
            "-t",
            f"{candidate.end - candidate.start:.6f}",
            "-map",
            "0:V:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            "pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-r",
            "5",
            *video_options(settings, 28),
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-ar",
            str(ASR_SAMPLE_RATE),
            "-b:a",
            "48k",
            "-movflags",
            "+faststart",
            destination,
        ],
        timeout=300,
        stage="preview",
    )
    if result.stderr.strip():
        raise PipelineError("preview reported a decoding error")
    return probe(destination)


def frame_size(settings):
    numerator, denominator = map(int, settings.aspect.split(":"))
    height = settings.height
    width = round(height * numerator / denominator / 2) * 2
    return width, height


def _box(box):
    """Crop expression for normalized x, y, width, height of the source frame."""
    x, y, w, h = box
    return f"crop=iw*{w:.6f}:ih*{h:.6f}:iw*{x:.6f}:ih*{y:.6f}"


def _video_timeline(clip, keep):
    if not keep:
        return "[0:V:0]setpts=PTS-STARTPTS[vin]"
    parts = [
        f"[0:V:0]trim=start={a - clip.start:.6f}:end={b - clip.start:.6f},"
        f"setpts=PTS-STARTPTS[vk{i}]"
        for i, (a, b) in enumerate(keep)
    ]
    labels = "".join(f"[vk{i}]" for i in range(len(keep)))
    return ";".join(parts) + f";{labels}concat=n={len(keep)}:v=1:a=0[vin]"


def _audio_timeline(clip, keep):
    if not keep:
        return "[0:a:0]asetpts=PTS-STARTPTS[ain]"
    parts = []
    for i, (a, b) in enumerate(keep):
        # Each kept piece fades briefly at both ends, so a splice where silence was removed
        # does not click when the background noise is not zero at that point.
        length = b - a
        fade = min(SPLICE_FADE, length / 2)
        parts.append(
            f"[0:a:0]atrim=start={a - clip.start:.6f}:end={b - clip.start:.6f},"
            f"asetpts=PTS-STARTPTS,afade=t=in:st=0:d={fade:.3f},"
            f"afade=t=out:st={length - fade:.3f}:d={fade:.3f}[ak{i}]"
        )
    labels = "".join(f"[ak{i}]" for i in range(len(keep)))
    return ";".join(parts) + f";{labels}concat=n={len(keep)}:v=0:a=1[ain]"


def _layout(settings, width, height, src, out):
    """Turn [src] into [out] for the selected layout; split labels stay internal."""
    if settings.layout == "blur":
        # Blur at quarter size, then enlarge: a cheap background on CPU runners.
        small_w, small_h = max(2, width // 4), max(2, height // 4)
        return (
            f"[{src}]split=2[bgs][fgs];"
            f"[bgs]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
            f"crop={small_w}:{small_h},boxblur=8:2,scale={width}:{height},"
            "eq=brightness=-0.06:saturation=0.9[bg];"
            f"[fgs]scale={width}:{height}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,setsar=1[fg];"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,setsar=1[{out}]"
        )
    if settings.layout == "panels":
        game_h = round(height * 0.6 / 2) * 2
        face_h = height - game_h
        game = settings.game_box or (0.0, 0.0, 1.0, 1.0)
        return (
            f"[{src}]split=2[gs][fs];"
            f"[gs]{_box(game)},scale={width}:{game_h}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2,pad={width}:{game_h}:(ow-iw)/2:(oh-ih)/2:black,"
            f"setsar=1[gp];"
            f"[fs]{_box(settings.face_box)},scale={width}:{face_h}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{face_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[fp];"
            f"[gp][fp]vstack=inputs=2,setsar=1[{out}]"
        )
    mode = "decrease" if settings.framing == "fit" else "increase"
    fit = (
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        if settings.framing == "fit"
        else f"crop={width}:{height}"
    )
    return (
        f"[{src}]scale={width}:{height}:force_original_aspect_ratio={mode}:"
        f"force_divisible_by=2,{fit},setsar=1[{out}]"
    )


def _chat_fit(settings, width, height, src, out, crop_x=None):
    """Fit [src] into the video area left of the chat column (width is that area's width).

    Used only with the chat panel, which replaces the layout and framing settings there. With
    fit top, [src] is the 16:9 picture box, so the letterbox branch fits it without bars.
    With fit fill, the picture is scaled to the area's height and cut to its width, starting
    at crop_x (a fraction of the source width, 0 to 1 minus the area's share); centred when
    crop_x is None.
    """
    if settings.chat_panel_fit == "fill":
        frame_width, _ = frame_size(settings)
        if crop_x is not None and not 0 <= crop_x <= 1 - width / frame_width + 1e-9:
            raise PipelineError("chat crop position is outside the source picture")
        left = f"(iw-{width})/2" if crop_x is None else f"iw*{crop_x:.6f}"
        fit = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:"
            f"force_divisible_by=2,crop={width}:{height}:{left}:(ih-{height})/2"
        )
    elif settings.chat_panel_fit == "crop":
        fit = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:"
            f"force_divisible_by=2,crop={width}:{height}"
        )
    else:
        fit = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:"
            f"color={CHAT_BACKGROUND}"
        )
    return f"[{src}]{fit},setsar=1[{out}]"


def _position(position, margin, top):
    return {
        "right": f"main_w-overlay_w-{margin}:(main_h-overlay_h)/2",
        "top": f"(main_w-overlay_w)/2:{top}",
        "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
        "bottom": f"(main_w-overlay_w)/2:main_h-overlay_h-{margin}",
    }[position]


def _video_graph(
    clip,
    settings,
    width,
    height,
    keep,
    duration,
    visuals,
    ass,
    chat_ass=None,
    crop_x=None,
):
    """Video chain from [0:V:0] to [vout]; visuals are inputs 1..len(visuals).

    With the chat panel, the picture and its overlays use the video area left of the chat
    column. The column is padded on after them, so their positions are relative to the area.
    With fit top the picture is shorter than the frame; overlays and progress use its height.
    """
    panel = settings.chat_panel_pixels()
    area = width - panel
    picture = settings.chat_video_height() if panel else height
    zooms = settings.zoom
    labels = [f"b{i}" for i in range(1 + len(zooms))]
    chain = [_video_timeline(clip, keep)]
    if len(labels) > 1:
        chain.append(
            "[vin]split=" + str(len(labels)) + "".join(f"[{x}]" for x in labels)
        )
        base = labels[0]
    else:
        base = "vin"
    if panel:
        chain.append(_chat_fit(settings, area, picture, base, "lay0", crop_x))
    else:
        chain.append(_layout(settings, width, height, base, "lay0"))
    previous = "lay0"
    for index, zoom in enumerate(zooms):
        # Enlarged face shown over the frame while the zoom interval is active.
        hh = min(
            round(picture * 0.8 / 2) * 2, round(picture * 0.3 * zoom.scale / 2) * 2
        )
        chain.append(
            f"[{labels[index + 1]}]{_box(settings.face_box)},"
            f"scale=w='min({area},trunc(iw*{hh}/ih/2)*2)':h={hh},setsar=1[z{index}]"
        )
        nxt = f"lay{index + 1}"
        chain.append(
            f"[{previous}][z{index}]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2:"
            f"enable='between(t,{zoom.start:.3f},{zoom.end:.3f})'[{nxt}]"
        )
        previous = nxt
    margin = round(picture * 0.04)
    # A top overlay sits below the heading when one is shown, so the title stays readable.
    top = round(picture * 0.14) if settings.heading else margin
    for index, visual in enumerate(visuals):
        max_w = round(area * visual["max_fraction"])
        # An overlay fades in and out over its own interval; a short one fades by half.
        ramp = min(settings.fade_seconds, (visual["end"] - visual["start"]) / 2)
        fades = ""
        if ramp > 0:
            fades = (
                f",fade=t=in:st={visual['start']:.3f}:d={ramp:.3f}:alpha=1"
                f",fade=t=out:st={visual['end'] - ramp:.3f}:d={ramp:.3f}:alpha=1"
            )
        chain.append(
            f"[{index + 1}:v]format=rgba,scale=w='min(iw,{max_w})':h=-2{fades}"
            f"[vi{index}]"
        )
        nxt = f"ov{index}"
        chain.append(
            f"[{previous}][vi{index}]overlay={_position(visual['position'], margin, top)}:"
            f"enable='between(t,{visual['start']:.3f},{visual['end']:.3f})'[{nxt}]"
        )
        previous = nxt
    fade = settings.fade_seconds
    if fade > 0 and duration > 2 * fade:
        chain.append(
            f"[{previous}]fade=t=in:st=0:d={fade:.3f},"
            f"fade=t=out:st={duration - fade:.3f}:d={fade:.3f}[fd]"
        )
        previous = "fd"
    if settings.progress_bar:
        # drawbox ignores the timestamp in its width, so the bar is a strip whose
        # alpha grows with T (seconds) across the frame width; only 8 rows are evaluated.
        chain.append(
            f"color=c=black@0:s={area}x8:r=30:d={duration:.6f},format=rgba,"
            f"geq=r={PROGRESS_RGB[0]}:g={PROGRESS_RGB[1]}:b={PROGRESS_RGB[2]}:"
            f"a='if(lte(X\\,{area}*T/{duration:.6f})\\,230\\,0)'[pbar]"
        )
        chain.append(f"[{previous}][pbar]overlay=0:{picture - 8}:shortest=1[pb]")
        previous = "pb"
    tail = []
    if panel:
        tail.append(f"pad={width}:{height}:0:0:color={CHAT_BACKGROUND}")
        tail.append(f"drawbox=x={area}:y=0:w=2:h={height}:color={CHAT_BORDER}:t=fill")
        if picture < height:
            # The caption band is separated from the picture by a line of the same colour.
            tail.append(
                f"drawbox=x=0:y={picture}:w={area}:h=2:color={CHAT_BORDER}:t=fill"
            )
    if ass:
        # Captions are copied to a fixed basename beside the generated MP4.
        tail.append("ass=filename=captions.ass")
    if chat_ass:
        # The chat is copied beside the MP4 under the same fixed-name rule as captions.
        tail.append("ass=filename=chat.ass")
    chain.append(f"[{previous}]{','.join(tail) or 'null'}[vout]")
    return ";".join(chain)


def _extra_inputs(settings, duration, visuals):
    """Argument list for visuals, background music and sound effects, in index order."""
    args = []
    for visual in visuals:
        args += [
            "-loop",
            "1",
            "-t",
            f"{duration:.6f}",
            *PROTOCOLS,
            "-i",
            visual["file"],
        ]
    if settings.bgm:
        args += [
            "-stream_loop",
            "-1",
            "-t",
            f"{duration:.6f}",
            *PROTOCOLS,
            "-i",
            settings.bgm,
        ]
    for effect in settings.sound_effects:
        args += [*PROTOCOLS, "-i", effect.file]
    return args


def _audio_graph(clip, settings, keep, duration, has_audio, first_extra):
    """Narration, optional ducked background music and sound effects, loudness-normalized."""
    if has_audio:
        base = _audio_timeline(clip, keep)
    else:
        base = f"anullsrc=r={AUDIO_RATE}:cl=mono,atrim=0:{duration:.6f},asetpts=PTS-STARTPTS[ain]"
    parts = [base]
    mix_inputs = []
    if settings.bgm:
        bgm_index = first_extra
        parts.append(
            f"[{bgm_index}:a]atrim=0:{duration:.6f},asetpts=PTS-STARTPTS,"
            f"volume={settings.bgm_gain_db:.2f}dB[bg0]"
        )
        if settings.ducking and has_audio:
            parts.append("[ain]asplit=2[nk][nm]")
            parts.append(
                "[bg0][nk]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=300[bgd]"
            )
        else:
            parts.append("[ain]anull[nm]")
            parts.append("[bg0]anull[bgd]")
        mix_inputs += ["[nm]", "[bgd]"]
        offset = first_extra + 1
    else:
        parts.append("[ain]anull[nm]")
        mix_inputs.append("[nm]")
        offset = first_extra
    for index, effect in enumerate(settings.sound_effects):
        parts.append(
            f"[{offset + index}:a]aresample={AUDIO_RATE},"
            f"volume={effect.gain_db:.2f}dB,adelay={round(effect.time * 1000)}:all=1[se{index}]"
        )
        mix_inputs.append(f"[se{index}]")
    if len(mix_inputs) > 1:
        parts.append(
            "".join(mix_inputs)
            + f"amix=inputs={len(mix_inputs)}:duration=first:normalize=0[mix]"
        )
    else:
        parts.append("[nm]anull[mix]")
    return ";".join(parts)


def _loudness_args(measured):
    text = f"loudnorm={LOUDNESS}"
    if measured:
        text += (
            f":measured_I={measured['input_i']:.2f}"
            f":measured_TP={measured['input_tp']:.2f}"
            f":measured_LRA={measured['input_lra']:.2f}"
            f":measured_thresh={measured['input_thresh']:.2f}"
            f":offset={measured['target_offset']:.2f}:linear=true"
        )
    return text


def _audio_tail(settings, duration, measured):
    fade = settings.fade_seconds
    tail = (
        f"[mix]{_loudness_args(measured)},aresample={AUDIO_RATE},"
        f"alimiter=limit={PEAK_LIMIT}:level=disabled:attack=5:release=50"
    )
    if fade > 0 and duration > 2 * fade:
        tail += f",afade=t=in:st=0:d={fade:.3f},afade=t=out:st={duration - fade:.3f}:d={fade:.3f}"
    return tail + "[aout]"


def measure_loudness(source, clip, settings, keep, duration):
    """First pass: measure the same audio chain (including music and effects) the render uses.

    Returns None when the audio is silent, so the render falls back to a single pass.
    """
    extra = _extra_inputs(settings, duration, [])
    graph = (
        _audio_graph(clip, settings, keep, duration, True, first_extra=1)
        + f";[mix]loudnorm={LOUDNESS}:print_format=json,aresample={AUDIO_RATE}[aout]"
    )
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "info",
        "-n",
        "-filter_complex_threads",
        str(encode_threads(settings)),
        "-ss",
        f"{clip.start:.6f}",
        *PROTOCOLS,
        *FORMATS,
        "-i",
        str(Path(source).resolve()),
        *extra,
        "-t",
        f"{duration:.6f}",
        "-filter_complex",
        graph,
        "-map",
        "[aout]",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(300, duration * 4),
        check=False,
    )
    blocks = re.findall(r"\{[^{}]*\}", result.stderr)
    if result.returncode or not blocks:
        raise PipelineError("loudness measurement failed")
    data = json.loads(blocks[-1])
    values = {}
    for key in ["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"]:
        number = float(data[key])
        if not math.isfinite(number):
            # Silence measures -inf and has no linear correction; one pass normalizes it.
            return None
        values[key] = number
    return values


def render(
    source,
    destination,
    clip,
    settings,
    ass=None,
    keep=None,
    visuals=(),
    chat_ass=None,
    crop_x=None,
):
    """Render one clip; visuals are dicts with file, start, end, position, max_fraction."""
    width, height = frame_size(settings)
    duration = sum(b - a for a, b in keep) if keep else clip.end - clip.start
    for zoom in settings.zoom:
        if zoom.end > duration + TIME_TOLERANCE:
            raise PipelineError("zoom interval is outside the rendered clip")
    for visual in visuals:
        if visual["end"] > duration + TIME_TOLERANCE:
            raise PipelineError("visual interval is outside the rendered clip")
        if not Path(visual["file"]).is_file():
            raise PipelineError("visual file is missing")
    if ass:
        # The graph names captions.ass by its basename, so only that beside the output is used.
        if Path(ass).resolve() != (Path(destination).parent / "captions.ass").resolve():
            raise PipelineError(
                "captions must be captions.ass beside the rendered clip"
            )
        if not Path(ass).is_file():
            raise PipelineError("captions file is missing")
    if chat_ass:
        if not settings.chat_panel:
            raise PipelineError("chat file needs the chat panel setting")
        if (
            Path(chat_ass).resolve()
            != (Path(destination).parent / "chat.ass").resolve()
        ):
            raise PipelineError("chat must be chat.ass beside the rendered clip")
        if not Path(chat_ass).is_file():
            raise PipelineError("chat file is missing")
    if settings.bgm and not Path(settings.bgm).is_file():
        raise PipelineError("background music file is missing")
    if len(settings.sound_effects) > MAX_SOUND_EFFECTS:
        raise PipelineError("too many sound effects")
    for effect in settings.sound_effects:
        if not Path(effect.file).is_file():
            raise PipelineError("sound effect file is missing")
        if effect.time >= duration:
            raise PipelineError("sound effect time is outside the rendered clip")
    if keep and len(keep) > MAX_KEEP_REGIONS:
        raise PipelineError("silence cut produced too many regions")
    source_info = probe(source)
    has_audio = source_info["audio"]
    first_extra = 1 + len(visuals)
    graph = [
        _video_graph(
            clip,
            settings,
            width,
            height,
            keep,
            duration,
            visuals,
            ass,
            chat_ass,
            crop_x,
        )
    ]
    if has_audio or settings.bgm or settings.sound_effects:
        measured = (
            measure_loudness(source, clip, settings, keep, duration)
            if settings.loudness_two_pass and has_audio
            else None
        )
        graph.append(
            _audio_graph(clip, settings, keep, duration, has_audio, first_extra)
        )
        graph.append(_audio_tail(settings, duration, measured))
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-n",
        "-ss",
        f"{clip.start:.6f}",
        "-threads",
        str(encode_threads(settings)),
        "-filter_threads",
        str(encode_threads(settings)),
        "-filter_complex_threads",
        str(encode_threads(settings)),
        *PROTOCOLS,
        *FORMATS,
        "-i",
        str(Path(source).resolve()),
        *_extra_inputs(settings, duration, visuals),
        "-t",
        f"{clip.end - clip.start:.6f}",
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[vout]",
    ]
    if has_audio or settings.bgm or settings.sound_effects:
        command += [
            "-map",
            "[aout]",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            str(AUDIO_RATE),
        ]
    command += [
        "-r",
        str(output_rate(source_info.get("frame_rate"))),
        *final_video_options(settings),
        "-movflags",
        "+faststart",
        str(Path(destination).resolve()),
    ]
    result = process(
        command,
        cwd=Path(destination).parent,
        timeout=max(300, duration * 20),
        stage="render",
    )
    if result.stderr.strip():
        raise PipelineError("clip rendering failed")
    info = probe(destination)
    if abs(info["duration"] - duration) > 0.3:
        raise PipelineError("rendered duration does not match the selection")
    if (info["width"], info["height"]) != (width, height):
        raise PipelineError("rendered dimensions do not match the requested layout")
    return info
