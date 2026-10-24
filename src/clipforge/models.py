"""Validated original-video seconds, job settings and model response contracts."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .storage import TIME_TOLERANCE

# Caption geometry in pixels at 720 px high; captions.py scales it with the frame height.
CAPTION_REFERENCE_HEIGHT = 720
CAPTION_MARGIN = 25
CAPTION_OUTLINE_ALLOWANCE = 8
# Line pitch of the caption font in em, measured on Noto Sans CJK JP (72 px lines at 72 px).
CAPTION_LINE_EM = 1.0


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Span(Record):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("start must be before end")
        return self


class Word(Span):
    text: str = Field(min_length=1, max_length=4000)


class Segment(Span):
    text: str = Field(max_length=16000)
    words: list[Word] = Field(default_factory=list)


class Transcript(Record):
    language: str
    segments: list[Segment]


class SelectedClip(Span):
    candidate_id: str
    title: str
    reason: str
    confidence: float = Field(ge=0, le=1)


class Zoom(Record):
    """Enlarged face over the rendered clip; start/end are output-timeline seconds."""

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    scale: float = Field(default=1.6, ge=1, le=2.5)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("zoom start must be before end")
        return self


class Visual(Record):
    """Image or SVG overlay in clip output seconds; file is a local path."""

    file: str = Field(min_length=1, max_length=1000)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    position: Literal["top", "center", "bottom", "right"] = "right"
    max_fraction: float = Field(default=0.4, ge=0.1, le=0.9)
    # Required for every file not created by ClipForge itself (source ledger).
    source_credit: str | None = Field(default=None, max_length=400)
    source_url: str | None = Field(default=None, max_length=1000)
    license: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("visual start must be before end")
        return self


class Diagram(Record):
    """Original table, bar chart or step diagram drawn by ClipForge."""

    kind: Literal["table", "bars", "steps"]
    title: str = Field(min_length=1, max_length=60)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    rows: list[list[str]] = Field(default_factory=list, max_length=12)
    labels: list[str] = Field(default_factory=list, max_length=8)
    values: list[float] = Field(default_factory=list, max_length=8)
    steps: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid(self):
        if self.end <= self.start:
            raise ValueError("diagram start must be before end")
        if self.kind == "table" and (
            not self.rows or not self.rows[0] or len({len(r) for r in self.rows}) != 1
        ):
            raise ValueError("table needs rows of equal, non-empty length")
        if self.kind == "bars" and (
            not self.labels or len(self.labels) != len(self.values)
        ):
            raise ValueError("bars need matching labels and values")
        if self.kind == "bars" and any(value < 0 for value in self.values):
            raise ValueError("bar values must not be negative")
        if self.kind == "steps" and not self.steps:
            raise ValueError("steps diagram needs at least one step")
        return self


class SoundEffect(Record):
    file: str = Field(min_length=1, max_length=1000)
    time: float = Field(ge=0)
    gain_db: float = Field(default=-18, ge=-60, le=-6)


class Settings(Record):
    clips: int = Field(default=3, ge=1, le=10)
    candidates: int = Field(default=8, ge=1, le=10)
    min_seconds: float = Field(default=20, ge=2, le=120)
    max_seconds: float = Field(default=75, ge=3, le=1800)
    # Colab GPU runtimes that recognise the audio side by side with Parakeet (asr_split).
    asr_parts: Literal[1, 2, 3, 4] = 4
    # Explicit hosted-runner audio acquisition route; not an automatic fallback.
    youtube_audio_transport: Literal["standard", "warp"] = "standard"
    # Folder of saved transcripts; a stage reads the one its analysis recorded.
    transcript_cache: str | None = Field(default=None, max_length=1024)
    require_transcript_cache: bool = False
    # A verified analysis's transcript key; only valid with require_transcript_cache.
    transcript_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    language: str | None = None
    video_encoder: Literal["auto", "libx264", "h264_nvenc"] = "auto"
    # Rendering only; 0 uses all cores available on the machine doing the render.
    encode_threads: int = Field(default=0, ge=0, le=1024)
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    framing: Literal["fit", "center-crop"] = "fit"
    # Frame height in pixels. Unset means the standard size for the aspect: 1920 for 9:16
    # (1080x1920, the size phones show at full sharpness), otherwise 1080.
    height: Literal[360, 720, 1080, 1920] | None = None
    # x264 speed preset. On a 40 s clip with CRF 18, slow matched medium's time and gained
    # about 0.1 dB PSNR; veryfast at CRF 20 was about 4 dB below it. Lower presets save time.
    encode_preset: Literal["superfast", "veryfast", "faster", "medium", "slow"] = "slow"
    caption: Literal["plain", "karaoke", "sequential"] = "karaoke"
    # Scales the caption font: 1.0 is 4.8 percent of the frame height.
    caption_scale: float = Field(default=1.0, ge=0.8, le=2.0)
    # Landscape only (aspect 16:9; ignored for 9:16). A caption is lifted above a busy static band
    # touching the bottom edge (auto_layout). False keeps every caption at the base margin.
    caption_lift: bool = True
    font: str = Field(default="Noto Sans CJK JP", min_length=1, max_length=100)
    horizontal_target_cps: float = Field(default=7, ge=1, le=20)
    horizontal_warn_cps: float = Field(default=7, ge=1, le=20)
    horizontal_line_chars: int = Field(default=13, ge=8, le=40)
    # blur fills the frame with a softened copy of the picture; fit leaves black bars.
    layout: Literal["fit", "blur", "panels"] = "blur"
    heading: bool = False
    # left pins the heading to the top-left corner; center is the default.
    heading_align: Literal["center", "left"] = "center"
    # Heading font scale; 1.0 is 7% of the frame height.
    heading_scale: float = Field(default=1.0, ge=0.8, le=1.6)
    face_box: tuple[float, float, float, float] | None = None
    game_box: tuple[float, float, float, float] | None = None
    zoom: list[Zoom] = Field(default_factory=list, max_length=5)
    workers: int = Field(default=2, ge=1, le=4)
    loudness_two_pass: bool = True
    silence_cut: bool = False
    silence_pad: float = Field(default=0.15, ge=0, le=1)
    silence_min_gap: float = Field(default=0.4, ge=0, le=3)
    vad: Literal["transcript", "silero"] = "transcript"
    topics: bool = True
    topic_min_seconds: float = Field(default=25, ge=5, le=120)
    topic_max_seconds: float = Field(default=180, ge=30, le=1800)
    visuals: list[Visual] = Field(default_factory=list, max_length=12)
    diagrams: list[Diagram] = Field(default_factory=list, max_length=8)
    image_search: bool = False
    image_sources: list[Literal["commons", "openverse"]] = Field(
        default_factory=lambda: ["commons"], max_length=2
    )
    # Searches Japanese Wikipedia tables for this query; off when unset.
    table_query: str | None = Field(default=None, min_length=1, max_length=100)
    bgm: str | None = Field(default=None, max_length=1000)
    bgm_gain_db: float = Field(default=-24, ge=-60, le=-12)
    ducking: bool = True
    sound_effects: list[SoundEffect] = Field(default_factory=list, max_length=20)
    thumbnail: bool = False
    fade_seconds: float = Field(default=0.3, ge=0, le=2)
    progress_bar: bool = True
    # strict refuses a clip whose quality.json lists failures; report keeps it and records them.
    quality_gate: Literal["report", "strict"] = "strict"
    max_source_seconds: float = Field(default=3600, ge=5, le=43200)
    max_input_bytes: int = Field(default=1073741824, ge=1024, le=2147483648)
    # Temporary HLS transport may contain video even when only audio is retained.
    max_transport_bytes: int = Field(default=6 * 1024**3, ge=1024, le=16 * 1024**3)
    chat_url: str | None = None
    chat_offset_seconds: float = Field(default=0, ge=-300, le=300)
    # Chat column beside the video of a landscape 16:9 render; the chat itself comes from a
    # Kick VOD URL (kick_chat). Off by default, so existing renders are unchanged.
    chat_panel: bool = False
    chat_panel_width: int = Field(default=480, ge=1, le=2000)
    # fill: the picture covers the video area and captions sit over it (moved to the top when
    # they would cover the action). top: a 16:9 frame above a caption band.
    chat_panel_fit: Literal["crop", "letterbox", "top", "fill"] = "fill"
    chat_anonymize: bool = False

    def chat_panel_pixels(self):
        """Width of the chat column at the right of the frame; 0 when the column is off."""
        return self.chat_panel_width if self.chat_panel else 0

    def chat_video_height(self):
        """Picture height: the whole frame, or with fit top the 16:9 picture above the band."""
        if not self.chat_panel or self.chat_panel_fit != "top":
            return self.height
        area = round(self.height * 16 / 9 / 2) * 2 - self.chat_panel_width
        return round(area * 9 / 16 / 2) * 2

    def caption_font_size(self):
        """Caption font in pixels; caption_scale multiplies it.

        With fit top the captions are in the band below the picture: the font is at most 6.7
        percent of the height (72 px at 1080), and two lines must fit in 80 percent of the
        band. With fit fill the captions sit over the picture, so the font is the same 6.7
        percent with no band to fit.
        """
        if self.chat_panel and self.chat_panel_fit == "fill":
            return round(self.height * 0.0667 * self.caption_scale)
        if not self.chat_panel or self.chat_panel_fit != "top":
            return round(self.height * 0.048 * self.caption_scale)
        band = self.height - self.chat_video_height()
        fit = math.floor(band * 0.8 / (2 * CAPTION_LINE_EM))
        return min(round(self.height * 0.0667 * self.caption_scale), fit)

    def caption_line_chars(self):
        """Landscape caption line limit, scaled to the video area left of the chat column."""
        if not self.chat_panel:
            return self.horizontal_line_chars
        frame = round(self.height * 16 / 9 / 2) * 2
        area = frame - self.chat_panel_width
        if self.chat_panel_fit in ("top", "fill"):
            # Characters that fit the picture width at the caption font, with the margins
            # that captions.py uses.
            scale = self.height / CAPTION_REFERENCE_HEIGHT
            usable = (
                area
                - 2 * round(CAPTION_MARGIN * scale)
                - round(CAPTION_OUTLINE_ALLOWANCE * scale)
            )
            return max(8, math.floor(usable / self.caption_font_size()))
        return max(8, math.floor(self.horizontal_line_chars * area / frame))

    @field_validator("face_box", "game_box")
    @classmethod
    def normalized_box(cls, value):
        if value is None:
            return value
        x, y, w, h = value
        if min(value) < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
            raise ValueError(
                "boxes are x, y, width, height in 0..1 of the source frame"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def standard_height(cls, data):
        if isinstance(data, dict) and data.get("height") is None:
            aspect = data.get("aspect", "9:16")
            data = {**data, "height": 1920 if aspect == "9:16" else 1080}
        return data

    @field_validator("asr_parts", mode="before")
    @classmethod
    def integer_parts(cls, value):
        if type(value) is not int or value not in (1, 2, 3, 4):
            raise ValueError("asr_parts must be the integer 1, 2, 3 or 4")
        return value

    @model_validator(mode="after")
    def limits(self):
        if self.horizontal_target_cps > self.horizontal_warn_cps:
            raise ValueError("horizontal reading target must not exceed warning limit")
        if self.require_transcript_cache and not self.transcript_cache:
            raise ValueError("requiring a saved transcript needs transcript_cache")
        if self.transcript_key and not self.require_transcript_cache:
            raise ValueError("a pinned transcript key is read only; require the cache")
        if self.height == 1920 and self.aspect != "9:16":
            raise ValueError("height 1920 is only for the 9:16 aspect")
        if self.chat_panel:
            if self.aspect != "16:9":
                raise ValueError("chat panel is only for landscape 16:9 output")
            frame = round(self.height * 16 / 9 / 2) * 2
            if self.chat_panel_width % 2 or not (
                0.2 * frame <= self.chat_panel_width <= 0.4 * frame
            ):
                raise ValueError(
                    "chat panel width must be an even 20 to 40 percent of the frame width"
                )
            if self.silence_cut:
                # Kick chat is timed on the original video; cut silence would shift it.
                raise ValueError(
                    "chat panel cannot be combined with silence_cut: the chat would drift"
                )
        if self.min_seconds >= self.max_seconds:
            raise ValueError("minimum duration must be less than maximum")
        if self.topic_max_seconds < 2 * self.topic_min_seconds:
            # Splitting an overlong topic in two must leave both halves at least the minimum.
            raise ValueError("topic maximum must be at least twice the topic minimum")
        if self.layout == "panels" and self.face_box is None:
            raise ValueError("layout panels needs face_box")
        if self.zoom and self.face_box is None:
            raise ValueError("zoom needs face_box")
        if self.candidates < self.clips:
            raise ValueError("candidate count must cover requested clips")
        if any(c in self.font for c in "\r\n,\\{}"):
            raise ValueError("invalid font name")
        return self


def validate_picks(picks, duration, settings):
    if not 1 <= len(picks) <= settings.clips:
        raise ValueError("invalid selected clip count")
    accepted = []
    for pick in picks:
        if not math.isfinite(pick.end) or pick.end > duration + TIME_TOLERANCE:
            raise ValueError("selected clip is outside the video")
        if (
            not settings.min_seconds - 0.1
            <= pick.end - pick.start
            <= settings.max_seconds + 0.1
        ):
            raise ValueError("selected clip duration is outside configured limits")
        for other in accepted:
            overlap = max(0, min(pick.end, other.end) - max(pick.start, other.start))
            if overlap / min(pick.end - pick.start, other.end - other.start) > 0.5:
                raise ValueError("selected clips are substantially duplicated")
        accepted.append(pick)
    return accepted
