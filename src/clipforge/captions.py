"""Phrase-aware ASS, progressive reveal and matching editable SRT in local seconds."""

from dataclasses import dataclass
import re

from .layout import output_size
from .storage import PipelineError
from .typography import boundaries, resolve_font, safe, with_breaks, wrap_offsets


@dataclass
class Token:
    start: float
    end: float
    text: str
    estimated: bool


def tokens(transcript, clip, font, line_width):
    result, warnings = [], set()
    for segment in transcript.segments:
        if segment.end <= clip.start or segment.start >= clip.end:
            continue
        source = segment.words or [segment]
        texts = [safe(w.text) for w in source]
        preferred, offset = boundaries("".join(texts)), 0
        for word, text in zip(source, texts, strict=True):
            cuts = sorted(
                {0, len(text)}
                | {i - offset for i in preferred if offset < i < offset + len(text)}
            )
            offset += len(text)
            if not text or word.end <= clip.start or word.start >= clip.end:
                continue
            fragments = []
            for left, right in zip(cuts, cuts[1:]):
                ends, forced = wrap_offsets(text[left:right], font, line_width)
                if forced:
                    warnings.add("forced_line_break")
                previous = left
                for end in ends:
                    fragments.append((previous, left + end))
                    previous = left + end
            estimated = not segment.words or len(fragments) > 1
            if estimated:
                warnings.add("estimated_caption_timing")
            if word.start < clip.start or word.end > clip.end:
                warnings.add("clip_boundary_inside_asr_token")
            for left, right in fragments:
                start = word.start + (word.end - word.start) * left / len(text)
                end = word.start + (word.end - word.start) * right / len(text)
                start, end = (
                    max(start, clip.start) - clip.start,
                    min(end, clip.end) - clip.start,
                )
                if end > start:
                    result.append(Token(start, end, text[left:right], estimated))
    result.sort(key=lambda t: (t.start, t.end))
    if any(a.end > b.start + 0.02 for a, b in zip(result, result[1:])):
        warnings.add("overlapping_asr_timing")
    return result, warnings


def group_tokens(items, font, line_width, max_seconds):
    groups, current = [], []
    for token in items:
        if current:
            text = "".join(t.text for t in current)
            ends, _ = wrap_offsets(text + token.text, font, line_width)
            if (
                len(ends) > 2
                or token.end - current[0].start > max_seconds
                or token.start - current[-1].end > 0.5
                or re.search(r"[。！？!?][」』）\"']?\s*$", text)
            ):
                groups.append(current)
                current = []
        current.append(token)
    if current:
        groups.append(current)
    return groups


def ass_text(group, offsets, mode):
    result, cursor = [], 0
    for index, token in enumerate(group):
        if mode == "karaoke":
            until = group[index + 1].start if index + 1 < len(group) else token.end
            # Absolute rounded offsets avoid cumulative centisecond drift.
            duration = max(
                0,
                round((until - group[0].start) * 100)
                - round((token.start - group[0].start) * 100),
            )
            result.append(r"{\kf" + str(duration) + "}")
        for char in token.text:
            if cursor in offsets[:-1]:
                result.append(r"\N")
            result.append(char)
            cursor += 1
    return "".join(result)


def progressive_events(group, offsets, start, end, outline):
    """Hide future fills AND outlines; fixed invisible placeholders prevent reflow."""
    import pysubs2

    text = "".join(t.text for t in group)
    starts = sorted(
        {start}
        | {round(t.start * 1000) for t in group if start < round(t.start * 1000) < end}
    )
    for reveal_index, begin in enumerate(starts):
        until = starts[reveal_index + 1] if reveal_index + 1 < len(starts) else end
        visible_count = sum(
            len(t.text) for t in group if round(t.start * 1000) <= begin
        )
        rendered, cursor = [], 0
        for token in group:
            visible = cursor < visible_count
            rendered.append(
                r"{\alpha&H00&\bord" + f"{outline:.2f}" + "}"
                if visible
                else r"{\alpha&HFF&\bord0}"
            )
            for char in token.text:
                if cursor in offsets[:-1]:
                    rendered.append(r"\N")
                rendered.append(char)
                cursor += 1
        yield (
            pysubs2.SSAEvent(start=begin, end=until, text="".join(rendered)),
            pysubs2.SSAEvent(
                start=begin, end=until, text=with_breaks(text[:visible_count], offsets)
            ),
        )


def create(transcript, clip, directory, settings):
    import pysubs2

    width, height = output_size(settings)
    # These margins are defaults, not universal TikTok/Shorts UI specifications.
    left, right = round(width * 0.07), round(width * 0.16)
    font_size = max(16, round(height * settings.caption_font_scale))
    outline = max(1.5, height * 0.0025)
    available = width - left - right - outline * 2 - 4
    headline = settings.headline
    if headline is None and settings.show_title and clip.candidate_id != "manual":
        headline = clip.title
    if not settings.show_title:
        headline = None
    has_text = bool(headline) or any(
        s.text.strip() or s.words
        for s in transcript.segments
        if s.end > clip.start and s.start < clip.end
    )
    face = resolve_font(settings.font) if has_text else None
    font = face.at(font_size) if face else None
    # ASS sizes against ascent+descent, Pillow measures against the em square.
    # Compensate so measured Japanese text and libass burn-in use the same scale.
    line_height = sum(font.getmetrics()) if font else font_size
    if settings.framing == "stacked" and 2 * line_height + 2 * outline > height * 0.12:
        raise PipelineError("subtitle font is too large for the stacked caption space")
    items, warnings = tokens(transcript, clip, font, available) if face else ([], set())
    groups = (
        group_tokens(items, font, available, settings.caption_max_seconds)
        if items
        else []
    )
    subtitles, plain = pysubs2.SSAFile(), pysubs2.SSAFile()
    subtitles.info.update(
        PlayResX=str(width),
        PlayResY=str(height),
        WrapStyle="2",
        ScaledBorderAndShadow="yes",
    )
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname=settings.font,
        fontsize=line_height,
        primarycolor=pysubs2.Color(255, 255, 255),
        secondarycolor=pysubs2.Color(255, 210, 40),
        outlinecolor=pysubs2.Color(0, 0, 0),
        outline=outline,
        shadow=0,
        bold=True,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginl=left,
        marginr=right,
        marginv=round(height * 0.14),
    )
    timings = []
    for index, group in enumerate(groups):
        text = "".join(t.text for t in group)
        offsets, forced = wrap_offsets(text, font, available)
        if forced:
            warnings.add("forced_line_break")
        start = round(group[0].start * 1000)
        end = min(
            round(max(t.end for t in group) * 1000),
            round((clip.end - clip.start) * 1000),
        )
        if index + 1 < len(groups):
            end = min(end, round(groups[index + 1][0].start * 1000))
        if end <= start:
            warnings.add("nonpositive_caption_interval")
            continue
        if settings.caption == "progressive":
            for event, editable in progressive_events(
                group, offsets, start, end, outline
            ):
                subtitles.append(event)
                plain.append(editable)
        else:
            subtitles.append(
                pysubs2.SSAEvent(
                    start=start,
                    end=end,
                    text=ass_text(group, offsets, settings.caption),
                )
            )
            plain.append(
                pysubs2.SSAEvent(start=start, end=end, text=with_breaks(text, offsets))
            )
        if any(t.estimated for t in group):
            timings.append({"start": start / 1000, "end": end / 1000})
    missing = (
        face.missing("".join(t.text for t in items) + safe(headline or ""))
        if face
        else []
    )
    if missing:
        warnings.add("font_missing_glyphs")
    if headline:
        title = safe(headline)
        size = round(height * 0.042)
        minimum = max(14, round(height * 0.026))
        while True:
            title_font = face.at(size)
            ends, forced = wrap_offsets(title, title_font, available)
            if len(ends) <= 2 or size <= minimum:
                break
            size -= 1
        if len(ends) > 2:
            raise PipelineError("headline does not fit two lines; shorten it")
        if forced:
            warnings.add("forced_headline_line_break")
        style = subtitles.styles["Default"].copy()
        style.fontsize, style.outline = sum(title_font.getmetrics()), outline
        style.primarycolor = pysubs2.Color(255, 210, 40)
        style.alignment, style.marginv = (
            pysubs2.Alignment.TOP_CENTER,
            round(height * 0.07),
        )
        subtitles.styles["Headline"] = style
        subtitles.append(
            pysubs2.SSAEvent(
                start=0,
                end=round((clip.end - clip.start) * 1000),
                text=with_breaks(title, ends),
                style="Headline",
                layer=1,
            )
        )
    ass, srt = directory / "captions.ass", directory / "captions.srt"
    subtitles.save(str(ass), encoding="utf-8")
    plain.save(str(srt), encoding="utf-8")
    return {
        "ass": ass.name,
        "srt": srt.name,
        "events": len(plain),
        "mode": settings.caption,
        "font": {"family": face.family, "style": face.style} if face else None,
        "missing_glyphs": missing,
        "estimated_timing_spans": timings,
        "review_required": sorted(warnings),
        "headline_rendered": bool(headline),
        "safe_margins": {"left": left, "right": right, "bottom": round(height * 0.14)},
    }
