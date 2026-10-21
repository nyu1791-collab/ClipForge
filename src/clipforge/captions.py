"""ASS/SRT captions and top heading in clip-relative output time; no user override tags."""

import pysubs2

from . import fonts, silence
from .models import Word
from .storage import PipelineError

MARGIN = 35
OUTLINE_ALLOWANCE = 8
YELLOW = (255, 210, 40)
_PARSER = None


def safe(text):
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def parser():
    """Phrase boundaries for Japanese text (BudouX, no network or model download)."""
    global _PARSER
    if _PARSER is None:
        import budoux

        _PARSER = budoux.load_default_japanese_parser()
    return _PARSER


def collect_tokens(transcript, clip, keep):
    """Word tokens (start, end, text) in output time, split where cuts removed audio."""
    tokens, approximate = [], 0
    for segment in transcript.segments:
        if segment.end <= clip.start or segment.start >= clip.end:
            continue
        source = segment.words or [
            Word(start=segment.start, end=segment.end, text=segment.text or " ")
        ]
        for word in source:
            text = safe(word.text)
            if not text.strip():
                continue
            a, b = max(clip.start, word.start), min(clip.end, word.end)
            if b <= a:
                continue
            spans = (
                silence.pieces(a, b, keep) if keep else [(a - clip.start, b - clip.start, 0)]
            )
            if not spans:
                continue
            if len(spans) > 1:
                # A token crossing a cut: divide its text in proportion to kept time.
                approximate += 1
                total = sum(e - s for s, e, _ in spans) or 1.0
                cursor = 0
                for index, (s, e, _) in enumerate(spans):
                    if index == len(spans) - 1:
                        stop = len(text)
                    else:
                        stop = cursor + round(len(text) * (e - s) / total)
                    part = text[cursor:stop]
                    cursor = stop
                    if part:
                        tokens.append((s, e, part))
            else:
                s, e, _ = spans[0]
                tokens.append((s, e, text))
    tokens.sort(key=lambda token: (token[0], token[1]))
    return tokens, approximate


def split_long(tokens, measure, limit):
    """Divide tokens wider than one line; the split is proportional in time."""
    result, split = [], 0
    for start, end, text in tokens:
        if measure.width(text) <= limit or len(text) <= 1:
            result.append((start, end, text))
            continue
        split += 1
        chunks, pending = [], ""
        for char in text:
            if pending and measure.width(pending + char) > limit:
                chunks.append(pending)
                pending = ""
            pending += char
        if pending:
            chunks.append(pending)
        total = sum(len(chunk) for chunk in chunks) or 1
        cursor, consumed = start, 0
        for chunk in chunks:
            consumed += len(chunk)
            until = start + (end - start) * consumed / total
            result.append((cursor, until, chunk))
            cursor = until
    return result, split


def wrap_breaks(text, measure, line_px):
    """Character indices where a new line starts; lines break at phrase edges when possible."""
    phrase_starts = set()
    position = 0
    for phrase in parser().parse(text):
        if position:
            phrase_starts.add(position)
        position += len(phrase)
    breaks, begin, used, last_phrase = [], 0, 0.0, None
    for index, char in enumerate(text):
        size = measure.width(char)
        if index > begin and index in phrase_starts:
            last_phrase = index
        if used + size > line_px and index > begin:
            cut = last_phrase if last_phrase is not None and last_phrase > begin else index
            breaks.append(cut)
            begin = cut
            used = measure.width(text[cut:index])
            last_phrase = None
        used += size
    return breaks


def group(tokens, measure, line_px, max_lines=2):
    """Caption groups that fit in max_lines, with pauses and long runs ending a group."""
    groups, current = [], []
    for token in tokens:
        if current:
            joined = "".join(item[2] for item in current) + token[2]
            too_many_lines = len(wrap_breaks(joined, measure, line_px)) + 1 > max_lines
            too_long = token[1] - current[0][0] > 4
            pause = token[0] - current[-1][1] > 0.7
            if too_many_lines or too_long or pause:
                groups.append(current)
                current = []
        current.append(token)
    if current:
        groups.append(current)
    return groups


def group_text(tokens, measure, line_px, mode):
    """ASS text for one group; mode is plain, karaoke (fill) or sequential (reveal)."""
    text = "".join(token[2] for token in tokens)
    breaks = set(wrap_breaks(text, measure, line_px))
    out, offset = [], 0
    for index, (start, end, token_text) in enumerate(tokens):
        if mode != "plain":
            begin = tokens[0][0] if index == 0 else start
            until = tokens[index + 1][0] if index + 1 < len(tokens) else end
            centiseconds = max(1, round((until - begin) * 100))
            tag = r"\k" if mode == "sequential" else r"\kf"
            out.append("{" + tag + str(centiseconds) + "}")
        for char_index, char in enumerate(token_text):
            if offset + char_index in breaks:
                out.append(r"\N")
            out.append(char)
        offset += len(token_text)
    return "".join(out)


def heading_size(title, frame_width, height, font_path):
    """Largest size that fits one line, shrinking to 60%; longer titles must be shortened."""
    base = round(height * 0.07)
    limit = frame_width - 2 * MARGIN
    for size in range(base, max(12, round(base * 0.6)) - 1, -1):
        if fonts.Measure(font_path, size).width(title) <= limit:
            return size
    raise PipelineError("heading does not fit the frame; shorten the clip title")


def create(transcript, clip, directory, settings, keep=None):
    height = settings.height
    numerator, denominator = map(int, settings.aspect.split(":"))
    frame_width = round(height * numerator / denominator / 2) * 2
    font_size = round(height * 0.048)
    font_path = fonts.resolve(settings.font)
    measure = fonts.Measure(font_path, font_size)
    line_px = frame_width - 2 * MARGIN - OUTLINE_ALLOWANCE

    tokens, approximate = collect_tokens(transcript, clip, keep)
    tokens, split = split_long(tokens, measure, line_px)
    approximate += split
    groups = group(tokens, measure, line_px)

    subtitles = pysubs2.SSAFile()
    subtitles.info.update(
        PlayResX=str(frame_width),
        PlayResY=str(height),
        WrapStyle="2",
        ScaledBorderAndShadow="yes",
    )
    base = dict(
        fontname=settings.font,
        fontsize=font_size,
        primarycolor=pysubs2.Color(255, 255, 255),
        outlinecolor=pysubs2.Color(0, 0, 0),
        outline=2.5,
        shadow=0.7,
        bold=True,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginl=MARGIN,
        marginr=MARGIN,
        marginv=round(height * 0.12),
    )
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        secondarycolor=pysubs2.Color(*YELLOW), **base
    )
    # Secondary alpha 255 hides text that has not been revealed yet, keeping layout stable.
    subtitles.styles["Sequential"] = pysubs2.SSAStyle(
        secondarycolor=pysubs2.Color(*YELLOW, 255), **base
    )

    heading_text = None
    title = getattr(clip, "title", None)
    if settings.heading and title:
        heading_text = safe(title)
        duration = sum(b - a for a, b in keep) if keep else clip.end - clip.start
        size = heading_size(heading_text, frame_width, height, font_path)
        subtitles.styles["Heading"] = pysubs2.SSAStyle(
            fontname=settings.font,
            fontsize=size,
            primarycolor=pysubs2.Color(*YELLOW),
            outlinecolor=pysubs2.Color(0, 0, 0),
            outline=3,
            shadow=0.5,
            bold=True,
            alignment=pysubs2.Alignment.TOP_CENTER,
            marginl=MARGIN,
            marginr=MARGIN,
            marginv=round(height * 0.06),
        )
        subtitles.append(
            pysubs2.SSAEvent(
                start=0,
                end=round(duration * 1000),
                style="Heading",
                text=heading_text,
            )
        )

    style = "Sequential" if settings.caption == "sequential" else "Default"
    plain_lines = []
    previous_end = 0
    for items in groups:
        start = max(previous_end, round(items[0][0] * 1000))
        end = round(items[-1][1] * 1000)
        if end <= start:
            continue
        text = group_text(items, measure, line_px, settings.caption)
        subtitles.append(
            pysubs2.SSAEvent(start=start, end=end, style=style, text=text)
        )
        plain_lines.append("".join(item[2] for item in items))
        previous_end = end

    corpus = "".join(plain_lines) + (heading_text or "")
    missing = fonts.missing_glyphs(font_path, corpus)

    ass, srt = directory / "captions.ass", directory / "captions.srt"
    subtitles.save(str(ass), encoding="utf-8")
    # SRT keeps only spoken subtitles; the heading is not dialogue and has no ASS tags.
    plain = pysubs2.SSAFile()
    for event in subtitles:
        if event.style == "Heading":
            continue
        plain.append(
            pysubs2.SSAEvent(start=event.start, end=event.end, text=event.plaintext)
        )
    plain.save(str(srt), encoding="utf-8")
    return {
        "ass": ass.name,
        "srt": srt.name,
        "events": len(plain),
        "heading": heading_text is not None,
        "approximate_tokens": approximate,
        "missing_glyphs": missing,
        "caption_mode": settings.caption,
    }
