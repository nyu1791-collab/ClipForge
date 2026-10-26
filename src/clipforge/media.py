"""Local, self-contained media only. No shell interpolation or input overwrites."""

import json
import math
from fractions import Fraction
from pathlib import Path

from .hardware import encode_threads, video_options
from .storage import PipelineError, process

PROTOCOLS = ["-protocol_whitelist", "file,pipe"]
FORMATS = ["-format_whitelist", "mov,matroska,avi,flv,mpeg,mpegts,ogg,asf,wav,flac,mp3"]
AUDIO_RATE = 48000
ASR_SAMPLE_RATE = 16000
# One-pass voice target, not a measured delivery guarantee or platform requirement.
LOUDNESS = "I=-14:TP=-1.5:LRA=11"
# Preserve the existing conservative sample limiter after voice + effects mixing.
# Completed AAC LUFS / true peak are not rescanned or used as pass/fail gates.
PEAK_LIMIT = 0.7079
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


def _video_graph(
    clip,
    settings,
    width,
    height,
    duration,
    ass,
    chat_ass=None,
    crop_x=None,
):
    """Video chain from the source to [vout], with the progress bar and captions.

    With the chat panel, the picture and progress bar use the video area left of the chat
    column. The column is padded on after them, so their positions are relative to the area.
    With fit top the picture is shorter than the frame; progress uses its height.
    """
    panel = settings.chat_panel_pixels()
    area = width - panel
    picture = settings.chat_video_height() if panel else height
    chain = ["[0:V:0]setpts=PTS-STARTPTS[vin]"]
    if panel:
        chain.append(_chat_fit(settings, area, picture, "vin", "lay0", crop_x))
    else:
        chain.append(_layout(settings, width, height, "vin", "lay0"))
    previous = "lay0"
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


def _audio_graph():
    """The narration on the clip clock, labelled [mix]."""
    return "[0:a:0]asetpts=PTS-STARTPTS[mix]"


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


def _audio_tail(settings, duration, measured, sound_effects=None):
    fade = settings.fade_seconds
    tail = f"[mix]{_loudness_args(measured)},aresample={AUDIO_RATE}"
    if sound_effects:
        from .sound_effects import mix_graph

        tail += ",aformat=channel_layouts=stereo[voice];"
        tail += mix_graph(sound_effects, duration, AUDIO_RATE) + ";[accented]"
    else:
        tail += ","
    tail += f"alimiter=limit={PEAK_LIMIT}:level=disabled:attack=5:release=50"
    if fade > 0 and duration > 2 * fade:
        tail += f",afade=t=in:st=0:d={fade:.3f},afade=t=out:st={duration - fade:.3f}:d={fade:.3f}"
    return tail + "[aout]"


def render(
    source,
    destination,
    clip,
    settings,
    ass=None,
    chat_ass=None,
    crop_x=None,
    sound_effects=None,
):
    """Render one clip with its captions and, optionally, the chat column."""
    width, height = frame_size(settings)
    duration = clip.end - clip.start
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
    source_info = probe(source)
    has_audio = source_info["audio"]
    graph = [
        _video_graph(
            clip,
            settings,
            width,
            height,
            duration,
            ass,
            chat_ass,
            crop_x,
        )
    ]
    if has_audio:
        from .sound_effects import prepare_cues

        graph.append(_audio_graph())
        graph.append(
            _audio_tail(settings, duration, None, prepare_cues(sound_effects, duration))
        )
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
        "-t",
        f"{clip.end - clip.start:.6f}",
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[vout]",
    ]
    if has_audio:
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
