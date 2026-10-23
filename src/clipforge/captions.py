"""ASS/SRT captions and top heading in clip-relative output time; no user override tags."""

import math
import re
import unicodedata

import pysubs2

from . import fonts, silence
from .asr_cleanup import clean_segments
from .caption_breaks import (
    NO_LINE_START,
    NO_LINE_END,
    BREAK_AFTER,
    PARTICLES,
    MAIN_PARTICLES,
    MIN_FILL,
    MIN_CAPTION_SECONDS,
    parser,
    NUMBER,
    LATIN,
    URL,
    _unbreakable,
    _is_combining,
    _is_kanji,
    _is_hiragana,
    _is_katakana,
    _kind,
    _boundary_rank,
    inside_word,
    wrap_breaks,
    _balanced_break,
    _word_cut,
)
from .models import (
    CAPTION_LINE_EM,
    CAPTION_MARGIN,
    CAPTION_OUTLINE_ALLOWANCE,
    CAPTION_REFERENCE_HEIGHT,
)
from .caption_sentences import (
    _tagger,
    _ends_sentence,
    sentence_ends,
    orphan_start,
)
from .models import Word
from .storage import PipelineError

__all__ = [
    "NO_LINE_START",
    "NO_LINE_END",
    "BREAK_AFTER",
    "PARTICLES",
    "MAIN_PARTICLES",
    "MIN_FILL",
    "MIN_CAPTION_SECONDS",
    "parser",
    "NUMBER",
    "LATIN",
    "URL",
    "_unbreakable",
    "_is_combining",
    "_is_kanji",
    "_is_hiragana",
    "_is_katakana",
    "_kind",
    "_boundary_rank",
    "inside_word",
    "wrap_breaks",
    "_balanced_break",
    "_word_cut",
    "_tagger",
    "_ends_sentence",
    "sentence_ends",
    "orphan_start",
]
# The margins below are in pixels at this height; models.py holds the values.
REFERENCE_HEIGHT = CAPTION_REFERENCE_HEIGHT
# Side margin of captions and headings. Narrower than before: the caption line is then long
# enough that fewer captions split words or end up as fragments (measured on the sample).
MARGIN = CAPTION_MARGIN
MIN_EVENT_MS = 100
# Reading speed in characters a second. A caption faster than the target stays on screen
# into the silence after it; one still faster than the maximum is reported as a warning.
TARGET_CAPTION_CPS = 10
MAX_CAPTION_CPS = 15
OUTLINE_ALLOWANCE = CAPTION_OUTLINE_ALLOWANCE
# Bottom margin of a caption, as a share of the height. With fit fill auto_layout may lift a
# caption above a busy band; the base is what it returns when nothing needs room.
FILL_BOTTOM_SHARE = 0.12
# Horizontal (landscape) line length and reading speed are defined only in Settings
# (horizontal_line_chars, horizontal_target_cps); this module reads them from the settings.
# A pause this long ends a caption. Shorter captions are joined to a neighbour across
# shorter pauses; no caption is shown for less than MIN_CAPTION_SECONDS when avoidable.
PAUSE_SECONDS = 0.7
JOIN_PAUSE_SECONDS = (
    0.7  # longest silence across which a short caption may join its neighbour
)
# Latin terms that speech recognition often writes in lower case, with their usual spelling.
TERMS = {
    "ai": "AI",
    "agi": "AGI",
    "asi": "ASI",
    "api": "API",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "cpu": "CPU",
    "gemini": "Gemini",
    "github": "GitHub",
    "gpt": "GPT",
    "gpu": "GPU",
    "llm": "LLM",
    "ollama": "Ollama",
    "openai": "OpenAI",
    "twitch": "Twitch",
    "youtube": "YouTube",
}
# Terms written as two or more words by recognition, joined into one caption token. A word
# must match exactly, with no punctuation, so the join never changes a sentence's end.
PHRASES = {
    ("chat", "gpt"): "ChatGPT",
    ("hugging", "face"): "Hugging Face",
    ("lm", "studio"): "LM Studio",
    ("open", "ai"): "OpenAI",
    ("stable", "diffusion"): "Stable Diffusion",
    ("you", "tube"): "YouTube",
}
YELLOW = (255, 210, 40)


def safe(text):
    # Canonical composition: a base character and its separate voiced mark become one
    # character, so the line breaker never sees the two apart.
    text = unicodedata.normalize("NFC", text)
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def normalize_terms(text):
    """Writes the Latin terms in TERMS in their usual capitals, e.g. ai becomes AI."""
    return re.sub(
        r"[A-Za-z]+",
        lambda match: TERMS.get(match.group(0).lower(), match.group(0)),
        text,
    )


def normalize_punctuation(text):
    """Caption typography only: full-width marks and one full-width following space."""
    text = text.translate(str.maketrans({"?": "？", "!": "！"}))
    return re.sub(r"([！？]+)[ \u3000]*", r"\1　", text)


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
            text = normalize_punctuation(normalize_terms(safe(word.text)))
            if not text.strip():
                continue
            a, b = max(clip.start, word.start), min(clip.end, word.end)
            if b <= a:
                continue
            spans = (
                silence.pieces(a, b, keep)
                if keep
                else [(a - clip.start, b - clip.start, 0)]
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
    for index in range(1, len(tokens)):
        if tokens[index - 1][2].endswith("　"):
            start, end, text = tokens[index]
            tokens[index] = (start, end, text.lstrip(" 　"))
    return join_phrases(tokens), approximate


def join_phrases(tokens):
    """Join adjacent tokens that spell a term in PHRASES into one token.

    The joined token starts with the first token's start and ends with the last token's end,
    so the timing is kept. The leading space of the first token is kept too.
    """
    joined, index = [], 0
    while index < len(tokens):
        for words, term in PHRASES.items():
            run = tokens[index : index + len(words)]
            if len(run) == len(words) and all(
                token[2].strip().lower() == word for token, word in zip(run, words)
            ):
                lead = re.match(r"\s*", run[0][2]).group(0)
                joined.append((run[0][0], run[-1][1], lead + term))
                index += len(words)
                break
        else:
            joined.append(tokens[index])
            index += 1
    return joined


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


def _lasts_a_caption(members):
    """True when members have lasted MIN_CAPTION_SECONDS, so they may close as a caption."""
    return members[-1][1] - members[0][0] >= MIN_CAPTION_SECONDS


def group(tokens, measure, line_px, max_lines=2):
    """Caption groups that fit in max_lines.

    A pause ends a group. When the text overflows, the group ends at the last word boundary
    before the overflow, so no word is split between two captions; the rest carries over.
    """

    def overflows(members, token):
        joined = "".join(item[2] for item in members) + token[2]
        too_many_lines = len(wrap_breaks(joined, measure, line_px)) + 1 > max_lines
        return too_many_lines or token[1] - members[0][0] > 4

    final = sentence_ends(tokens)
    groups, current = [], []
    for index, token in enumerate(tokens):
        if (
            current
            and token[0] - current[-1][1] > PAUSE_SECONDS
            or current
            and index - 1 in final
            and _lasts_a_caption(current)
        ):
            groups.append(current)
            current = []
        while current and overflows(current, token):
            cut = _word_cut(current) or len(current)
            groups.append(current[:cut])
            current = current[cut:]
        current.append(token)
    if current:
        groups.append(current)
    return _join_short(groups, measure, line_px, max_lines)


def _fits_caption(members, measure, line_px, max_lines):
    text = "".join(item[2] for item in members)
    within_lines = len(wrap_breaks(text, measure, line_px)) + 1 <= max_lines
    return within_lines and members[-1][1] - members[0][0] <= 4


def _join_short(groups, measure, line_px, max_lines):
    """Join a caption shorter than MIN_CAPTION_SECONDS to a neighbour, when that still fits.

    Only neighbours with no pause between them qualify. A short caption that cannot join
    stays short; it is never made longer than the caption rules allow.
    """
    merged = list(groups)
    index = 0
    while index < len(merged) and len(merged) > 1:
        members = merged[index]
        if members[-1][1] - members[0][0] >= MIN_CAPTION_SECONDS:
            index += 1
            continue
        options = []
        if index > 0 and members[0][0] - merged[index - 1][-1][1] <= JOIN_PAUSE_SECONDS:
            options.append((index - 1, merged[index - 1] + members))
        if (
            index + 1 < len(merged)
            and merged[index + 1][0][0] - members[-1][1] <= JOIN_PAUSE_SECONDS
        ):
            options.append((index, members + merged[index + 1]))
        joined = next(
            (
                (position, combined)
                for position, combined in options
                if _fits_caption(combined, measure, line_px, max_lines)
            ),
            None,
        )
        if joined is None:
            index += 1
            continue
        position, combined = joined
        merged[position : position + 2] = [combined]
        index = position
    return merged


def with_breaks(text, breaks):
    """Text with a hard ASS line break before each character index in breaks."""
    return "".join(
        r"\N" + char if index in breaks else char for index, char in enumerate(text)
    )


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
    # SRT strips outer whitespace; retain punctuation spacing inside the caption only.
    return "".join(out).rstrip()


def spoken_length(text):
    """Characters a viewer reads; spaces and line breaks do not count."""
    return len(re.sub(r"\s", "", text))


def hold_for_reading(
    spans, texts, limit, cps=TARGET_CAPTION_CPS, floor_ms=0, lead_ms=0
):
    """Extend captions read faster than the target into the silence after them.

    spans holds [start, end] in milliseconds, in order, and is changed in place. Only
    ends move, and never past the next caption's start or the clip's end, so no caption
    starts later than its speech or overlaps another. A caption is held for at least
    floor_ms, when the room allows.
    """
    for index, (span, text) in enumerate(zip(spans, texts)):
        ceiling = spans[index + 1][0] if index + 1 < len(spans) else max(limit, span[1])
        reading = math.ceil(spoken_length(text) * 1000 / cps)
        wanted = span[0] + max(reading, floor_ms)
        # ASS keeps centiseconds and SRT milliseconds, so a held end is rounded up to a whole
        # centisecond (never short of the reading time) without passing the ceiling; speech
        # times already are whole centiseconds, and both files then keep the same timing.
        held = min(-(-wanted // 10) * 10, ceiling - ceiling % 10)
        span[1] = max(span[1], held)
    if lead_ms:
        for index, (span, text) in enumerate(zip(spans, texts)):
            needed = math.ceil(spoken_length(text) * 1000 / cps) - (span[1] - span[0])
            previous_end = spans[index - 1][1] if index else 0
            room = max(0, min(lead_ms, span[0] - previous_end))
            # Use only nearby silence, on centisecond boundaries, without moving speech.
            shift = min(room, max(0, -(-needed // 10) * 10))
            span[0] -= shift - shift % 10
    return spans


def scale(height):
    """Pixels per reference pixel: margins and outlines were tuned at 720 px high."""
    return height / REFERENCE_HEIGHT


def margin_px(height):
    return round(MARGIN * scale(height))


def heading_size(title, frame_width, height, font_path):
    """Largest size that fits one line, shrinking to 60%; longer titles must be shortened."""
    base = round(height * 0.07)
    limit = frame_width - 2 * margin_px(height)
    for size in range(base, max(12, round(base * 0.6)) - 1, -1):
        if fonts.Measure(font_path, size).width(title) <= limit:
            return size
    raise PipelineError("heading does not fit the frame; shorten the clip title")


def _caption_layout(settings):
    """Font, measure and line width of the captions; shared by create and event_spans."""
    height = settings.height
    margin = margin_px(height)
    numerator, denominator = map(int, settings.aspect.split(":"))
    frame_width = round(height * numerator / denominator / 2) * 2
    font_size = settings.caption_font_size()
    font_path = fonts.resolve(settings.font)
    measure = fonts.Measure(font_path, font_size)
    # With the chat panel the video area is narrower than the frame.
    area = frame_width - settings.chat_panel_pixels()
    line_px = area - 2 * margin - round(OUTLINE_ALLOWANCE * scale(height))
    landscape = frame_width > height
    if landscape:
        line_px = min(
            line_px, round(measure.width("あ" * settings.caption_line_chars()))
        )
    return font_size, font_path, measure, line_px, landscape


def _caption_groups(transcript, clip, settings, keep, measure, line_px, landscape):
    """Caption groups, their (start, end) in ms on the output timeline, and cleanup info."""
    duration = sum(b - a for a, b in keep) if keep else clip.end - clip.start
    segments, cleanup = clean_segments(transcript.segments)
    caption_transcript = transcript.model_copy(update={"segments": segments})
    tokens, approximate = collect_tokens(caption_transcript, clip, keep)
    tokens, split = split_long(tokens, measure, line_px)
    approximate += split
    groups = group(tokens, measure, line_px)
    plain_lines = ["".join(item[2] for item in items) for items in groups]
    spans, previous_end = [], 0
    for items in groups:
        # Proportional word splits can create fractional centiseconds. ASS and SRT must
        # share the same representable times before holding captions into silence.
        start = max(previous_end, round(items[0][0] * 100) * 10)
        # A group squeezed out by the previous one is kept for a moment, never dropped.
        end = max(round(items[-1][1] * 100) * 10, start + MIN_EVENT_MS)
        spans.append([start, end])
        previous_end = end
    hold_for_reading(
        spans,
        plain_lines,
        round(duration * 1000),
        settings.horizontal_target_cps if landscape else TARGET_CAPTION_CPS,
        floor_ms=round(MIN_CAPTION_SECONDS * 1000),
        lead_ms=500
        if landscape
        and any(
            _is_kanji(c) or _is_hiragana(c) or _is_katakana(c)
            for text in plain_lines
            for c in text
        )
        else 0,
    )
    return groups, spans, plain_lines, cleanup, approximate


def event_spans(transcript, clip, settings, keep=None):
    """Start and end in seconds, on the output timeline, of each caption create() writes."""
    _, _, measure, line_px, landscape = _caption_layout(settings)
    groups, spans, _, _, _ = _caption_groups(
        transcript, clip, settings, keep, measure, line_px, landscape
    )
    return [(start / 1000, end / 1000) for start, end in spans]


def _lift_margins(lifts, count, height):
    """MarginV in pixels for each caption; a list that does not match the captions gives the base."""
    if lifts is None or len(lifts) != count:
        lifts = [FILL_BOTTOM_SHARE] * count
    return [round(lift * height) for lift in lifts]


def create(transcript, clip, directory, settings, keep=None, credits=None, lifts=None):
    """Write captions.ass and captions.srt.

    lifts gives the bottom margin of each caption as a share of the height, in order. Given lifts
    place every caption bottom centre (alignment 2) at its own margin; a list that does not match
    the captions gives every caption the base margin. Without lifts, only fill places captions
    this way, as before.
    """
    height = settings.height
    margin = margin_px(height)
    numerator, denominator = map(int, settings.aspect.split(":"))
    frame_width = round(height * numerator / denominator / 2) * 2
    font_size, font_path, measure, line_px, landscape = _caption_layout(settings)
    duration = sum(b - a for a, b in keep) if keep else clip.end - clip.start
    # With the chat panel, text stays in the video area left of the column: the right margin
    # grows by the column width, so ASS centres the text on that area.
    panel = settings.chat_panel_pixels()
    area = frame_width - panel
    margin_right = margin + panel
    # With fit top the picture is shorter than the frame; the rest is the caption band.
    band = height - settings.chat_video_height()
    fill = settings.chat_panel and settings.chat_panel_fit == "fill"
    # The outline is 2.5 px at the standard 4.8 percent font; fill scales it with its larger font.
    outline = round(2.5 * font_size / round(height * 0.048), 1) if fill else 2.5

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
        outline=outline,
        shadow=0.7,
        bold=True,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginl=margin,
        marginr=margin_right,
        marginv=round(height * FILL_BOTTOM_SHARE),
    )
    if band:
        # Two lines centred in the caption band below the picture.
        base["marginv"] = round((band - 2 * font_size * CAPTION_LINE_EM) / 2)
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
        size = heading_size(heading_text, area, height, font_path)
        subtitles.styles["Heading"] = pysubs2.SSAStyle(
            fontname=settings.font,
            fontsize=size,
            primarycolor=pysubs2.Color(*YELLOW),
            outlinecolor=pysubs2.Color(0, 0, 0),
            outline=3,
            shadow=0.5,
            bold=True,
            alignment=pysubs2.Alignment.TOP_CENTER,
            marginl=margin,
            marginr=margin_right,
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

    if credits:
        # Source credits for every image used in the clip; small, bottom-right, never in SRT.
        credit_size = max(14, round(height * 0.026))
        subtitles.styles["Credit"] = pysubs2.SSAStyle(
            fontname=settings.font,
            fontsize=credit_size,
            primarycolor=pysubs2.Color(235, 235, 235),
            outlinecolor=pysubs2.Color(0, 0, 0),
            outline=1.5,
            alignment=pysubs2.Alignment.BOTTOM_RIGHT,
            marginl=margin,
            marginr=margin_right,
            # Credits stay on the picture, above the bottom edge of the picture.
            marginv=band + round(height * 0.015),
        )
        credit_text = safe(" / ".join(credits))
        breaks = wrap_breaks(
            credit_text, fonts.Measure(font_path, credit_size), area - 2 * margin
        )
        subtitles.append(
            pysubs2.SSAEvent(
                start=0,
                end=round(duration * 1000),
                style="Credit",
                text=with_breaks(credit_text, breaks),
            )
        )

    groups, spans, plain_lines, cleanup, approximate = _caption_groups(
        transcript, clip, settings, keep, measure, line_px, landscape
    )
    style = "Sequential" if settings.caption == "sequential" else "Default"
    # Fill always places captions by their own MarginV. Elsewhere, lifts place them only without the
    # chat column (the chat's crop, letterbox and top fits keep the base margin); without lifts the
    # output is what it was before.
    placed = fill or (lifts is not None and not settings.chat_panel)
    margins = _lift_margins(lifts, len(groups), height) if placed else [0] * len(groups)
    for items, (start, end), margin in zip(groups, spans, margins):
        text = group_text(items, measure, line_px, settings.caption)
        if placed:
            # Bottom centre (\an2); the event's own MarginV sets how high the caption sits.
            event = pysubs2.SSAEvent(
                start=start,
                end=end,
                style=style,
                text=r"{\an2}" + text,
                marginv=margin,
            )
        else:
            event = pysubs2.SSAEvent(start=start, end=end, style=style, text=text)
        subtitles.append(event)

    corpus = "".join(plain_lines) + (heading_text or "") + "".join(credits or [])
    missing = fonts.missing_glyphs(font_path, corpus)

    ass, srt = directory / "captions.ass", directory / "captions.srt"
    subtitles.save(str(ass), encoding="utf-8")
    # SRT keeps only spoken subtitles; the heading is not dialogue and has no ASS tags.
    plain = pysubs2.SSAFile()
    for event in subtitles:
        if event.style in {"Heading", "Credit"}:
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
        "asr_cleanup": cleanup,
    }
