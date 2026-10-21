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


class Settings(Record):
    gemini_model: str = Field(min_length=1, max_length=160)
    clips: int = Field(default=3, ge=1, le=10)
    candidates: int = Field(default=8, ge=1, le=10)
    min_seconds: float = Field(default=20, ge=2, le=120)
    max_seconds: float = Field(default=75, ge=3, le=180)
    whisper_model: str = Field(default="small", min_length=1, max_length=160)
    whisper_revision: str | None = None
    language: str | None = None
    threads: int = Field(default=2, ge=1, le=8)
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    framing: Literal["fit", "center-crop"] = "fit"
    height: Literal[720, 1080] = 720
    caption: Literal["plain", "karaoke", "sequential"] = "karaoke"
    font: str = Field(default="Noto Sans CJK JP", min_length=1, max_length=100)
    layout: Literal["fit", "blur", "panels"] = "fit"
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
            raise ValueError("boxes are x, y, width, height in 0..1 of the source frame")
        return value

    @model_validator(mode="after")
    def limits(self):
        if self.min_seconds >= self.max_seconds:
            raise ValueError("minimum duration must be less than maximum")
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
