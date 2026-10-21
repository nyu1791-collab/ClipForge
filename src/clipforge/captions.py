"""ASS/SRT generation with measured Japanese word and phrase boundaries."""

import unicodedata

from . import caption_layout as layout
from .fonts import Measure
from .models import Word
from .storage import PipelineError, digest, write_json


def safe(text):
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def width(text):
    # Kept for callers that estimate terminal-style character cells.
    return sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in text)


def wrap_tokens(words, karaoke, start, line_cells):
    # Compatibility helper; production uses measured font widths in create().
    return _text(words, karaoke, start, width, line_cells)


def _text(words, karaoke, start, measure, limit):
    breaks = layout.line_break([w.text for w in words], measure, limit)
    if breaks is None:
        raise PipelineError("caption exceeds two measured lines")
    result = []
    for i, w in enumerate(words):
        if i in breaks:
            result.append(r"\N")
        if karaoke:
            begin = start if i == 0 else max(start, w.start)
            until = words[i + 1].start if i + 1 < len(words) else w.end
            result.append(r"{\kf" + str(max(1, round((until - begin) * 100))) + "}")
        result.append(w.text)
    return "".join(result)


def create(transcript, clip, directory, settings, fonts_dir=None):
    import pysubs2

    height = settings.height
    n, d = map(int, settings.aspect.split(":"))
    frame_width = round(height * n / d / 2) * 2
    font_size = round(height * (0.048 if settings.aspect == "9:16" else 0.052))
    measure = Measure(settings.font, font_size, fonts_dir)
    limit = frame_width - 86
    words = []
    for s in transcript.segments:
        if s.end <= clip.start or s.start >= clip.end:
            continue
        for w in s.words or [Word(start=s.start, end=s.end, text=s.text or " ")]:
            a, b = (
                max(clip.start, w.start) - clip.start,
                min(clip.end, w.end) - clip.start,
            )
            if b > a and w.text.strip():
                words.append(Word(start=a, end=b, text=layout.normalize(safe(w.text))))
    words.sort(key=lambda w: (w.start, w.end))
    words, approximated = layout.timed_units(words)
    groups = layout.groups(words, measure.width, limit)
    # SRT readers trim event-edge whitespace. Canonicalize it before either export
    # so an ASR word beginning with a space cannot make ASS/SRT disagree.
    for i, members in enumerate(groups):
        members = list(members)
        members[0] = members[0].model_copy(update={"text": members[0].text.lstrip()})
        members[-1] = members[-1].model_copy(update={"text": members[-1].text.rstrip()})
        groups[i] = members
    subtitles = pysubs2.SSAFile()
    subtitles.info.update(
        PlayResX=str(frame_width),
        PlayResY=str(height),
        WrapStyle="2",
        ScaledBorderAndShadow="yes",
    )
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        fontname=settings.font,
        fontsize=font_size,
        primarycolor=pysubs2.Color(255, 255, 255),
        secondarycolor=pysubs2.Color(255, 210, 40),
        outlinecolor=pysubs2.Color(0, 0, 0),
        outline=settings.caption_outline,
        shadow=0.7,
        bold=True,
        alignment=2,
        marginl=35,
        marginr=35,
        marginv=round(height * (0.12 if settings.aspect == "9:16" else 0.06)),
    )
    duration_ms = round((clip.end - clip.start) * 1000)
    if settings.headline:
        title = layout.normalize(safe(settings.hook_title or clip.title))
        for size in range(round(height * 0.052), round(height * 0.028) - 1, -1):
            title_measure = Measure(settings.font, size, fonts_dir)
            parts = layout.units(title)
            cuts = layout.line_break(parts, title_measure.width, frame_width - 90)
            if cuts is not None:
                break
        else:
            raise PipelineError("title exceeds two lines; shorten the title")
        title_text = "".join(
            (r"\N" if i in cuts else "") + p for i, p in enumerate(parts)
        )
        subtitles.styles["Headline"] = pysubs2.SSAStyle(
            fontname=settings.font,
            fontsize=size,
            primarycolor=pysubs2.Color(255, 220, 30),
            outlinecolor=pysubs2.Color(0, 0, 0),
            outline=3.5,
            shadow=1,
            bold=True,
            alignment=8,
            marginl=35,
            marginr=35,
            marginv=round(height * 0.05),
        )
        subtitles.append(
            pysubs2.SSAEvent(
                start=0, end=duration_ms, style="Headline", text=title_text, layer=1
            )
        )
    warnings, previous = [], 0
    for i, group in enumerate(groups):
        start = max(previous, round(group[0].start * 100) * 10)
        ceiling = (
            min(duration_ms, round(groups[i + 1][0].start * 100) * 10)
            if i + 1 < len(groups)
            else duration_ms
        )
        spoken = "".join(w.text for w in group)
        wanted = start + max(1000, round(len(spoken.strip()) * 1000 / 10))
        end = min(ceiling, start + 4000, max(round(group[-1].end * 1000), wanted))
        end = (
            end // 10 * 10
        )  # ASS uses centiseconds; SRT must keep identical boundaries.
        if end <= start:
            raise PipelineError("overlapping ASR timing leaves no caption duration")
        event_measure, prefix = measure.width, ""
        if layout.line_break([w.text for w in group], event_measure, limit) is None:
            scale = min(1, limit / measure.width(spoken))
            if scale < 0.6:
                raise PipelineError("caption word cannot fit at minimum font size")
            size = int(font_size * scale)
            event_measure = Measure(settings.font, size, fonts_dir).width
            prefix = r"{\fs" + str(size) + "}"
            warnings.append(
                {"event": i, "type": "long_word_font_reduced", "font_size": size}
            )
        text = prefix + _text(
            group, settings.caption == "karaoke", start / 1000, event_measure, limit
        )
        subtitles.append(pysubs2.SSAEvent(start=start, end=end, text=text))
        cps = len("".join(spoken.split())) * 1000 / (end - start)
        if cps > 15 or end - start < 1000:
            warnings.append(
                {
                    "event": i,
                    "start": start / 1000,
                    "seconds": (end - start) / 1000,
                    "cps": round(cps, 2),
                }
            )
        previous = end
    missing = measure.missing("".join(e.plaintext for e in subtitles))
    if missing:
        raise PipelineError("selected font lacks caption glyphs")
    ass, srt = directory / "captions.ass", directory / "captions.srt"
    subtitles.save(str(ass), encoding="utf-8")
    plain = pysubs2.SSAFile()
    for e in subtitles:
        if e.style != "Headline":
            plain.append(pysubs2.SSAEvent(start=e.start, end=e.end, text=e.plaintext))
    plain.save(str(srt), encoding="utf-8")
    report = {
        "ass": ass.name,
        "srt": srt.name,
        "events": len(plain),
        "max_lines": 2,
        "approximate_word_boundaries": approximated,
        "warnings": warnings,
        "font_sha256": digest(measure.path),
        "font_size": font_size,
        "line_pixels": limit,
        "layout_engine": "janome+budoux+measured-font",
        "missing_glyphs": missing,
    }
    write_json(directory / "caption_review.json", report)
    return report
