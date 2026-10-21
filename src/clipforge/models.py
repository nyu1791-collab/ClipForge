"""Validated original-video seconds, job settings and model response contracts."""

import itertools
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
    id: str
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


class SourceRegion(Record):
    """Rectangle in the displayed source, normalized to 0..1 (after rotation)."""

    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def contained(self):
        if self.x + self.width > 1 + 1e-9 or self.y + self.height > 1 + 1e-9:
            raise ValueError("source region must stay inside the frame")
        return self


class PunchIn(Span):
    """Fixed presenter enlargement; start/end remain ORIGINAL-video seconds."""

    zoom: float = Field(default=1.25, gt=1, le=1.6)


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
    framing: Literal["fit", "blur-fit", "center-crop", "stacked"] = "blur-fit"
    height: Literal[720, 1080, 1280, 1920] = 1920
    caption: Literal["plain", "karaoke", "progressive"] = "progressive"
    font: str = Field(default="Noto Sans CJK JP", min_length=1, max_length=100)
    caption_max_seconds: float = Field(default=2.8, ge=0.5, le=5)
    caption_font_scale: float = Field(default=0.036, ge=0.02, le=0.055)
    headline: str | None = Field(default=None, min_length=1, max_length=120)
    show_title: bool = True
    content_region: SourceRegion | None = None
    presenter_region: SourceRegion | None = None
    punch_ins: list[PunchIn] = Field(default_factory=list, max_length=8)
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

    @model_validator(mode="after")
    def limits(self):
        if self.min_seconds >= self.max_seconds:
            raise ValueError("minimum duration must be less than maximum")
        if self.candidates < self.clips:
            raise ValueError("candidate count must cover requested clips")
        if self.analysis_fps * self.max_seconds * self.candidates > 6000:
            raise ValueError("analysis frame budget exceeds 6000")
        if any(c in self.font for c in "\r\n,\\{}"):
            raise ValueError("invalid font name")
        if self.framing == "stacked":
            if self.aspect != "9:16" or self.presenter_region is None:
                raise ValueError("stacked framing needs 9:16 and a presenter_region")
        elif self.presenter_region or self.content_region or self.punch_ins:
            raise ValueError("source regions and punch-ins require stacked framing")
        intervals = sorted(self.punch_ins, key=lambda p: p.start)
        if any(a.end > b.start for a, b in itertools.pairwise(intervals)):
            raise ValueError("presenter punch-ins must not overlap")
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
