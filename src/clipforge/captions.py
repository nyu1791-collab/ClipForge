"""ASS/SRT captions in clip-relative time; no executable user override tags."""

import unicodedata

from .models import Word


def safe(text):
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def width(text):
    return sum(
        2 if unicodedata.east_asian_width(c) in {"W", "F"} or c in "MW@#%" else 1
        for c in text
    )


def wrap_tokens(words, karaoke, start, line_cells):
    result, line_width, lines = [], 0, 1
    for index, word in enumerate(words):
        text = safe(word.text)
        pieces = []
        for char in text:
            size = width(char)
            if line_width + size > line_cells:
                if lines < 2:
                    pieces.append(r"\N")
                    lines += 1
                else:
                    # A single ASR token may be an entire Japanese phrase. The group
                    # builder limits this case; preserve text instead of discarding it.
                    pieces.append(r"\N")
                line_width = 0
            pieces.append(char)
            line_width += size
        if karaoke:
            cursor = start if index == 0 else word.start
            end = words[index + 1].start if index + 1 < len(words) else word.end
            centiseconds = max(1, round((end - cursor) * 100))
            result.append(r"{\kf" + str(centiseconds) + "}")
        result.append("".join(pieces))
    return "".join(result).strip()


def create(transcript, clip, directory, settings):
    import pysubs2

    height = settings.height
    numerator, denominator = map(int, settings.aspect.split(":"))
    frame_width = round(height * numerator / denominator / 2) * 2
    font_size = round(height * settings.caption_size_ratio)
    margin = 35
    # Reserve space for outlines and differences between Latin/CJK font metrics.
    line_cells = max(12, min(52, int((frame_width - margin * 2) / (font_size * 0.6))))
    words = []
    for segment in transcript.segments:
        if segment.end <= clip.start or segment.start >= clip.end:
            continue
        source = segment.words or [
            Word(start=segment.start, end=segment.end, text=segment.text or " ")
        ]
        for word in source:
            start, end = (
                max(clip.start, word.start) - clip.start,
                min(clip.end, word.end) - clip.start,
            )
            if end > start and word.text.strip():
                # Divide overlong ASR tokens for layout only, proportionally in time.
                chunks, pending = [], ""
                for char in word.text:
                    if width(pending + char) > line_cells - 2 and pending:
                        chunks.append(pending)
                        pending = ""
                    pending += char
                if pending:
                    chunks.append(pending)
                total = max(1, sum(len(t) for t in chunks))
                cursor = start
                for chunk in chunks:
                    until = min(end, cursor + (end - start) * len(chunk) / total)
                    if until > cursor:
                        words.append(Word(start=cursor, end=until, text=chunk))
                    cursor = until
    words.sort(key=lambda w: (w.start, w.end))
    groups, current = [], []
    for word in words:
        if current and (
            width("".join(w.text for w in current) + word.text) > line_cells * 2 - 2
            or word.end - current[0].start > 4
            or word.start - current[-1].end > 0.7
        ):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
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
        marginv=round(height * 0.12),
    )
    if settings.headline:
        title = settings.hook_title or clip.title
        headline_size = round(height * 0.052)
        while True:
            title_cells = max(10, int((frame_width - 50) / (headline_size * 0.65)))
            lines, current = [], ""
            for char in safe(title):
                if width(current + char) > title_cells:
                    lines.append(current)
                    current = ""
                current += char
            if current:
                lines.append(current)
            if len(lines) <= 2 or headline_size <= round(height * 0.037):
                break
            headline_size -= 1
        # A hook must fit in two lines. Truncate explicitly rather than covering
        # the source with a long AI-generated title.
        if len(lines) > 2:
            lines = lines[:2]
            lines[1] = lines[1][:-1] + "…"
        subtitles.styles["Headline"] = pysubs2.SSAStyle(
            fontname=settings.font,
            fontsize=headline_size,
            primarycolor=pysubs2.Color(255, 220, 30),
            outlinecolor=pysubs2.Color(0, 0, 0),
            outline=settings.caption_outline,
            shadow=1,
            bold=True,
            alignment=8,
            marginl=25,
            marginr=25,
            marginv=round(height * 0.07),
        )
        subtitles.append(
            pysubs2.SSAEvent(
                start=0,
                end=round((clip.end - clip.start) * 1000),
                style="Headline",
                text=r"\N".join(lines),
                layer=1,
            )
        )
    previous_end = 0
    for group in groups:
        start = max(previous_end, round(group[0].start * 1000))
        end = min(round((clip.end - clip.start) * 1000), round(group[-1].end * 1000))
        if end <= start:
            continue
        subtitles.append(
            pysubs2.SSAEvent(
                start=start,
                end=end,
                text=wrap_tokens(
                    group, settings.caption == "karaoke", start / 1000, line_cells
                ),
            )
        )
        previous_end = end
    ass, directory_srt = directory / "captions.ass", directory / "captions.srt"
    subtitles.save(str(ass), encoding="utf-8")
    # SRT deliberately has no ASS overrides; exported text comes from plaintext.
    plain = pysubs2.SSAFile()
    for event in subtitles:
        if event.style == "Headline":
            continue
        plain.append(
            pysubs2.SSAEvent(start=event.start, end=event.end, text=event.plaintext)
        )
    plain.save(str(directory_srt), encoding="utf-8")
    return {"ass": ass.name, "srt": directory_srt.name, "events": len(subtitles)}
