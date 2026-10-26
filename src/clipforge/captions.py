"""ASS/SRT captions and top heading in clip-relative output time; no user override tags."""

import math
import re
import unicodedata

import pysubs2

from . import fonts
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
from .speaker_captions import SPEAKER_COLORS, speaker_for_token, validate_turns
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


JAPANESE_MARKS = str.maketrans({"?": "？", "!": "！"})
# A fullwidth space is needed after ？ or ！ unless the text after it closes a bracket or sentence.
MARK_FOLLOWERS_NOT_SPACED = "」』）)］】〉》、。…‥？！"


def japanese_marks(text):
    """Halfwidth ? and ! become fullwidth; inside a token, more text after them is spaced.

    The fullwidth space follows the same rule as space_after_marks between tokens.
    """
    return re.sub(
        r"([？！]+)[ \t　]*(?=[^\s" + re.escape(MARK_FOLLOWERS_NOT_SPACED) + "])",
        r"\1　",
        text.translate(JAPANESE_MARKS),
    )


def space_after_marks(tokens):
    """Adds a fullwidth space to a token ending in ？ or ！ when more text continues after it.

    Only the text is changed, so the timing of every token stays as it was.
    """
    spaced = list(tokens)
    for index in range(len(spaced) - 1):
        start, end, text = spaced[index]
        # A halfwidth space before the fullwidth one is replaced, so the result has no gap.
        # A token already ending in a fullwidth space ends in neither mark, so it is skipped.
        before = text.rstrip(" \t")
        if before[-1:] not in ("？", "！"):
            continue
        next_start, next_end, next_text = spaced[index + 1]
        following = next_text.lstrip(" \t")
        if not following or following[0].isspace():
            continue
        if following[0] in MARK_FOLLOWERS_NOT_SPACED:
            continue
        spaced[index] = (start, end, before + "　")
        # The leading halfwidth space of the next token would double the gap, so it goes.
        spaced[index + 1] = (next_start, next_end, following)
    return spaced


def collect_tokens(transcript, clip):
    """Word tokens (start, end, text), clipped and offset to the selected interval."""
    tokens, approximate = [], 0
    japanese = transcript.language == "ja"
    for segment in transcript.segments:
        if segment.end <= clip.start or segment.start >= clip.end:
            continue
        source = segment.words or [
            Word(start=segment.start, end=segment.end, text=segment.text or " ")
        ]
        for word in source:
            text = normalize_terms(safe(word.text))
            if japanese:
                text = japanese_marks(text)
            if not text.strip():
                continue
            a, b = max(clip.start, word.start), min(clip.end, word.end)
            if b <= a:
                continue
            tokens.append((a - clip.start, b - clip.start, text))
    tokens.sort(key=lambda token: (token[0], token[1]))
    tokens = join_phrases(tokens)
    if japanese:
        tokens = space_after_marks(tokens)
    return tokens, approximate


def pause_marks(tokens):
    """、 and 。 inside one caption become a fullwidth space; at its end they are dropped.

    Japanese captions show pauses as spaces. The marks are replaced only after grouping,
    because the line and caption breaks use them; token timing stays as it was.
    """
    texts = [token[2] for token in tokens]
    for index in reversed(range(len(texts))):
        texts[index] = texts[index].rstrip("、。 \t")
        if texts[index]:
            break
    texts = [re.sub(r"[、。]+", "　", text) for text in texts]
    for index in range(1, len(texts)):
        # One space between pauses: a token after a converted mark drops its own lead.
        if texts[index - 1].endswith("　"):
            texts[index] = texts[index].lstrip(" \t　")
    if not "".join(texts).strip():
        return tokens
    return [(start, end, text) for (start, end, _), text in zip(tokens, texts)]


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
        # Use the same lexical and punctuation rules as the displayed line breaks.
        # A raw width cut can otherwise leave a sentence mark as its own timed token.
        points = [0, *wrap_breaks(text, measure, limit), len(text)]
        chunks = [text[left:right] for left, right in zip(points, points[1:])]
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


def group(
    tokens,
    measure,
    line_px,
    max_lines=2,
    *,
    min_seconds=MIN_CAPTION_SECONDS,
    max_seconds=4,
    preferred_lines=2,
):
    """Caption groups that fit in max_lines.

    A pause ends a group. When the text overflows, the group ends at the last word boundary
    before the overflow, so no word is split between two captions; the rest carries over.
    """

    def overflows(members, token):
        joined = "".join(item[2] for item in members) + token[2]
        too_many_lines = len(wrap_breaks(joined, measure, line_px)) + 1 > max_lines
        return too_many_lines or token[1] - members[0][0] > max_seconds

    def finishes_in_extra_line(members, index):
        # Use the third line only when it can reach the next sentence/pause boundary.
        # Otherwise break at two lines, rather than filling three on every caption.
        trial = list(members)
        for following in range(index, len(tokens)):
            token = tokens[following]
            if overflows(trial, token):
                return False
            trial.append(token)
            if (
                following in final
                or following == len(tokens) - 1
                or tokens[following + 1][0] - token[1] > PAUSE_SECONDS
            ):
                return True
        return False

    final = sentence_ends(tokens)
    groups, current = [], []
    for index, token in enumerate(tokens):
        if (
            current
            and token[0] - current[-1][1] > PAUSE_SECONDS
            or current
            and index - 1 in final
            and current[-1][1] - current[0][0] >= min_seconds
        ):
            groups.append(current)
            current = []
        if (
            current
            and max_lines > preferred_lines
            and len(
                wrap_breaks("".join(t[2] for t in [*current, token]), measure, line_px)
            )
            + 1
            > preferred_lines
            and current[-1][1] - current[0][0] >= min_seconds
            and not finishes_in_extra_line(current, index)
        ):
            cut = _word_cut(current) or len(current)
            groups.append(current[:cut])
            current = current[cut:]
        while current and overflows(current, token):
            cut = _word_cut(current) or len(current)
            groups.append(current[:cut])
            current = current[cut:]
        current.append(token)
    if current:
        groups.append(current)
    return _join_short(groups, measure, line_px, max_lines, min_seconds, max_seconds)


def _fits_caption(members, measure, line_px, max_lines, max_seconds=4):
    text = "".join(item[2] for item in members)
    within_lines = len(wrap_breaks(text, measure, line_px)) + 1 <= max_lines
    return within_lines and members[-1][1] - members[0][0] <= max_seconds


def _join_short(
    groups, measure, line_px, max_lines, min_seconds=MIN_CAPTION_SECONDS, max_seconds=4
):
    """Join a caption shorter than MIN_CAPTION_SECONDS to a neighbour, when that still fits.

    Only neighbours with no pause between them qualify. A short caption that cannot join
    stays short; it is never made longer than the caption rules allow.
    """
    merged = list(groups)
    index = 0
    while index < len(merged) and len(merged) > 1:
        members = merged[index]
        if members[-1][1] - members[0][0] >= min_seconds:
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
                if _fits_caption(combined, measure, line_px, max_lines, max_seconds)
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
    # Recognised English words can keep a leading space. Strip caption edges before tags
    # are added, matching SRT's serialization without changing spacing between words.
    tokens = list(tokens)
    if tokens:
        first = tokens[0]
        tokens[0] = (first[0], first[1], first[2].lstrip())
        last = tokens[-1]
        tokens[-1] = (last[0], last[1], last[2].rstrip())
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


def heading_size(title, frame_width, height, font_path, scale_factor=1.0):
    """Largest size that fits one line, shrinking to 60%; longer titles must be shortened."""
    base = round(height * 0.07 * scale_factor)
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
    measure = (
        fonts.AssMeasure(font_path, font_size)
        if frame_width > height
        else fonts.Measure(font_path, font_size)
    )
    # With the chat panel the video area is narrower than the frame.
    area = frame_width - settings.chat_panel_pixels()
    line_px = area - 2 * margin - round(OUTLINE_ALLOWANCE * scale(height))
    landscape = frame_width > height
    if landscape:
        line_px = min(
            line_px, round(measure.width("あ" * settings.caption_line_chars()))
        )
    return font_size, font_path, measure, line_px, landscape


def _caption_groups(
    transcript, clip, settings, measure, line_px, landscape, speaker_turns=None
):
    """Caption groups, their (start, end) in ms on the output timeline, and cleanup info."""
    duration = clip.end - clip.start
    segments, cleanup = clean_segments(transcript.segments)
    caption_transcript = transcript.model_copy(update={"segments": segments})
    tokens, approximate = collect_tokens(caption_transcript, clip)
    tokens, split = split_long(tokens, measure, line_px)
    approximate += split
    turns = validate_turns(speaker_turns or [], duration)
    runs, previous_speaker = [], None
    for token in tokens:
        speaker = speaker_for_token(token[0], token[1], turns)
        if not runs or speaker != previous_speaker:
            runs.append([])
        runs[-1].append(token)
        previous_speaker = speaker
    options = (
        dict(
            max_lines=settings.horizontal_max_lines,
            min_seconds=settings.horizontal_min_caption_seconds,
            max_seconds=settings.horizontal_max_caption_seconds,
        )
        if landscape
        else {}
    )
    groups = [
        items for run in runs for items in group(run, measure, line_px, **options)
    ]
    if transcript.language == "ja":
        groups = [pause_marks(items) for items in groups]
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
        floor_ms=round(
            (
                settings.horizontal_min_caption_seconds
                if landscape
                else MIN_CAPTION_SECONDS
            )
            * 1000
        ),
    )
    return groups, spans, plain_lines, cleanup, approximate


def event_spans(transcript, clip, settings, speaker_turns=None):
    """Start and end in seconds, on the output timeline, of each caption create() writes."""
    _, _, measure, line_px, landscape = _caption_layout(settings)
    groups, spans, _, _, _ = _caption_groups(
        transcript, clip, settings, measure, line_px, landscape, speaker_turns
    )
    return [(start / 1000, end / 1000) for start, end in spans]


def _lift_margins(lifts, count, height):
    """MarginV in pixels for each caption; a list that does not match the captions gives the base."""
    if lifts is None or len(lifts) != count:
        lifts = [FILL_BOTTOM_SHARE] * count
    return [round(lift * height) for lift in lifts]


def create(transcript, clip, directory, settings, lifts=None, speaker_turns=None):
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
    duration = clip.end - clip.start
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
        base["marginv"] = round(
            (band - settings.horizontal_max_lines * font_size * CAPTION_LINE_EM) / 2
        )
    subtitles.styles["Default"] = pysubs2.SSAStyle(
        secondarycolor=pysubs2.Color(*YELLOW), **base
    )
    # Secondary alpha 255 hides text that has not been revealed yet, keeping layout stable.
    subtitles.styles["Sequential"] = pysubs2.SSAStyle(
        secondarycolor=pysubs2.Color(*YELLOW, 255), **base
    )
    for speaker, color in SPEAKER_COLORS.items():
        speaker_base = {**base, "primarycolor": pysubs2.Color(*color)}
        # Karaoke retains the speaker's hue on both sides of the fill; sequential
        # reveal keeps its transparent secondary color. The hue never cycles yellow.
        subtitles.styles[speaker] = pysubs2.SSAStyle(
            secondarycolor=pysubs2.Color(
                *color, 255 if settings.caption == "sequential" else 0
            ),
            **speaker_base,
        )

    heading_text = None
    title = getattr(clip, "title", None)
    if settings.heading and title:
        heading_text = safe(title)
        size = heading_size(
            heading_text, area, height, font_path, settings.heading_scale
        )
        subtitles.styles["Heading"] = pysubs2.SSAStyle(
            fontname=settings.font,
            fontsize=size,
            primarycolor=pysubs2.Color(*YELLOW),
            outlinecolor=pysubs2.Color(0, 0, 0),
            outline=3,
            shadow=0.5,
            bold=True,
            alignment=(
                pysubs2.Alignment.TOP_LEFT
                if settings.heading_align == "left"
                else pysubs2.Alignment.TOP_CENTER
            ),
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

    groups, spans, plain_lines, cleanup, approximate = _caption_groups(
        transcript, clip, settings, measure, line_px, landscape, speaker_turns
    )
    style = "Sequential" if settings.caption == "sequential" else "Default"
    # Fill always places captions by their own MarginV. Elsewhere, lifts place them only without the
    # chat column (the chat's crop, letterbox and top fits keep the base margin); without lifts the
    # output is what it was before.
    placed = fill or (lifts is not None and not settings.chat_panel)
    margins = _lift_margins(lifts, len(groups), height) if placed else [0] * len(groups)
    for items, (start, end), margin in zip(groups, spans, margins):
        speaker = speaker_for_token(items[0][0], items[0][1], speaker_turns or [])
        event_style = speaker or style
        text = group_text(items, measure, line_px, settings.caption)
        if placed:
            # Bottom centre (\an2); the event's own MarginV sets how high the caption sits.
            event = pysubs2.SSAEvent(
                start=start,
                end=end,
                style=event_style,
                text=r"{\an2}" + text,
                marginv=margin,
            )
        else:
            event = pysubs2.SSAEvent(start=start, end=end, style=event_style, text=text)
        subtitles.append(event)

    corpus = "".join(plain_lines) + (heading_text or "")
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
        "speaker_colored_events": sum(e.style in SPEAKER_COLORS for e in subtitles),
        "asr_cleanup": cleanup,
    }
