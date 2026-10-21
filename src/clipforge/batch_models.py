"""Batch manifest: every timestamp is measured in original-stream seconds."""

from typing import Literal

from pydantic import Field, model_validator

from .models import Record, Segment


class RichStyle(Record):
    height: Literal[720, 1080] = 720
    font: str = "Noto Sans CJK JP"
    fonts_dir: str | None = None
    caption_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    highlight_color: str = "#FFD228"
    bgm: str | None = None
    se: str | None = None
    overlay: str | None = None
    bgm_gain: float = Field(default=0.07, ge=0, le=0.3)
    se_gain: float = Field(default=0.15, ge=0, le=0.6)
    preset: Literal["slow", "medium", "veryfast"] = "slow"
    crf: int = Field(default=18, ge=14, le=28)


class BatchClip(Record):
    clip_id: str = Field(pattern=r"clip_0[1-5]")
    type: Literal["topic", "highlight"]
    hook_title: str = Field(min_length=1, max_length=100)
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    subtitles: list[Segment] = Field(default_factory=list, max_length=10000)
    score: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def interval(self):
        if not 30 <= self.end_time - self.start_time <= 1200:
            raise ValueError("each clip must last 30-1200 seconds")
        previous = self.start_time
        for s in self.subtitles:
            if s.start < previous - 0.01 or s.end > self.end_time + 0.01:
                raise ValueError(
                    "subtitles must be ordered inside the original-stream interval"
                )
            if any(w.start < s.start or w.end > s.end for w in s.words):
                raise ValueError("subtitle word exceeds segment")
            previous = s.end
        return self


class ClipsManifest(Record):
    schema_version: Literal[1] = 1
    source_url: str
    source_duration: float = Field(gt=0, le=86400)
    subtitle_timebase: Literal["source"] = "source"
    selection_method: Literal["test_intervals", "manual", "local_transcript_audio"]
    output_kind: Literal["shorts", "topics"] = "shorts"
    style: RichStyle = Field(default_factory=RichStyle)
    clips: list[BatchClip] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def consistency(self):
        from .kick_media import canonical

        canonical(self.source_url)
        expected = [f"clip_{i:02}" for i in range(1, len(self.clips) + 1)]
        if [c.clip_id for c in self.clips] != expected:
            raise ValueError(
                "clip IDs must be unique consecutive clip_01 through clip_05"
            )
        accepted = []
        for c in self.clips:
            lower, upper = (30, 180) if self.output_kind == "shorts" else (60, 1200)
            if not lower <= c.end_time - c.start_time <= upper:
                raise ValueError(f"{self.output_kind} duration must be {lower}-{upper} seconds")
            if self.output_kind == "topics" and c.type != "topic":
                raise ValueError("normal topic videos require topic intervals")
            if c.end_time > self.source_duration + 0.01:
                raise ValueError("clip exceeds original stream")
            for other in accepted:
                overlap = max(
                    0,
                    min(c.end_time, other.end_time)
                    - max(c.start_time, other.start_time),
                )
                if (
                    overlap
                    / min(c.end_time - c.start_time, other.end_time - other.start_time)
                    > 0.2
                ):
                    raise ValueError("clips overlap by more than 20 percent")
            accepted.append(c)
        return self
