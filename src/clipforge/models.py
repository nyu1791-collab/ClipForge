"""Validated original-video seconds, job settings and model response contracts."""

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class Candidate(Span):
    id: str = Field(pattern=r"^candidate-[0-9]{3}$")
    heuristic_score: float
    speech_seconds: float
    chat_reactions: float
    scene_boundaries: int


class LocalPick(Span):
    """start/end are local seconds in the candidate video, not the original."""

    candidate_id: str
    title: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class PickEnvelope(Record):
    clips: list[LocalPick] = Field(min_length=1, max_length=10)


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
    gemini_model: str = Field(min_length=1, max_length=160)
    clips: int = Field(default=3, ge=1, le=10)
    candidates: int = Field(default=8, ge=1, le=10)
    min_seconds: float = Field(default=20, ge=2, le=120)
    max_seconds: float = Field(default=75, ge=3, le=180)
    # large-v3-turbo transcribes Japanese clearly better than small at about the same speed
    # (measured on 4 minutes of speech: mean word probability 0.96 against 0.88).
    whisper_model: str = Field(
        default="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        min_length=1,
        max_length=160,
    )
    # None uses the pinned revision of the default model; other models float unless set.
    whisper_revision: str | None = None
    # 0 keeps sequential decoding (default); 1-16 batches speech chunks for speed.
    asr_batch_size: int = Field(default=0, ge=0, le=16)
    # Folder of saved transcripts; a run reuses one whose audio and recognition settings match.
    transcript_cache: str | None = Field(default=None, max_length=1024)
    require_transcript_cache: bool = False
    language: str | None = None
    whisper_device: Literal["auto", "cpu", "cuda"] = "auto"
    whisper_compute_type: Literal[
        "auto", "int8", "float16", "float32", "int8_float16"
    ] = "auto"
    video_encoder: Literal["auto", "libx264", "h264_nvenc"] = "auto"
    threads: int = Field(default=0, ge=0, le=1024)
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    framing: Literal["fit", "center-crop"] = "fit"
    # Frame height in pixels. Unset means the standard size for the aspect: 1920 for 9:16
    # (1080x1920, the size phones show at full sharpness), otherwise 1080.
    height: Literal[720, 1080, 1920] | None = None
    # x264 speed preset. On a 40 s clip with CRF 18, slow matched medium's time and gained
    # about 0.1 dB PSNR; veryfast at CRF 20 was about 4 dB below it. Lower presets save time.
    encode_preset: Literal["superfast", "veryfast", "faster", "medium", "slow"] = "slow"
    caption: Literal["plain", "karaoke", "sequential"] = "karaoke"
    font: str = Field(default="Noto Sans CJK JP", min_length=1, max_length=100)
    horizontal_target_cps: float = Field(default=7, ge=1, le=20)
    horizontal_warn_cps: float = Field(default=7, ge=1, le=20)
    horizontal_line_chars: int = Field(default=13, ge=8, le=40)
    # blur fills the frame with a softened copy of the picture; fit leaves black bars.
    layout: Literal["fit", "blur", "panels"] = "blur"
    heading: bool = False
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
    topic_max_seconds: float = Field(default=180, ge=30, le=600)
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
    analysis_fps: float = Field(default=2, ge=0.5, le=5)
    max_source_seconds: float = Field(default=3600, ge=5, le=7200)
    max_input_bytes: int = Field(default=1073741824, ge=1024, le=2147483648)
    chat_url: str | None = None
    chat_offset_seconds: float = Field(default=0, ge=-300, le=300)

    @field_validator("gemini_model")
    @classmethod
    def model_identifier(cls, value):
        if not re.fullmatch(r"(?:models/)?[A-Za-z0-9._-]+", value):
            raise ValueError("use an explicit Gemini API model identifier")
        return value

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

    @model_validator(mode="after")
    def limits(self):
        if self.horizontal_target_cps > self.horizontal_warn_cps:
            raise ValueError("horizontal reading target must not exceed warning limit")
        if self.require_transcript_cache and not self.transcript_cache:
            raise ValueError("requiring a saved transcript needs transcript_cache")
        if self.height == 1920 and self.aspect != "9:16":
            raise ValueError("height 1920 is only for the 9:16 aspect")
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
        if self.analysis_fps * self.max_seconds * self.candidates > 6000:
            raise ValueError("analysis frame budget exceeds 6000")
        if any(c in self.font for c in "\r\n,\\{}"):
            raise ValueError("invalid font name")
        return self


def validate_picks(picks, duration, settings):
    if not 1 <= len(picks) <= settings.clips:
        raise ValueError("invalid selected clip count")
    accepted = []
    for pick in picks:
        if not math.isfinite(pick.end) or pick.end > duration + 0.01:
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
