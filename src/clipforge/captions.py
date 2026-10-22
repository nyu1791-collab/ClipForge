"""ASS/SRT captions and top heading in clip-relative output time; no user override tags."""

import math
import re
import unicodedata

import pysubs2

from . import fonts, silence
from .models import Word
from .storage import PipelineError

REFERENCE_HEIGHT = 720  # the margins below are in pixels at this height
# Side margin of captions and headings. Narrower than before: the caption line is then long
# enough that fewer captions split words or end up as fragments (measured on the sample).
MARGIN = 25
MIN_EVENT_MS = 100
# Reading speed in characters a second. A caption faster than the target stays on screen
# into the silence after it; one still faster than the maximum is reported as a warning.
TARGET_CAPTION_CPS = 10
MAX_CAPTION_CPS = 15
OUTLINE_ALLOWANCE = 8
# Characters that may not start a line: closing marks, small kana, long vowel, spaces.
NO_LINE_START = set(
    "、。，．・：；！？!?」』）〕］｝ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶー 　"
)
# Characters that may not end a line: opening brackets.
NO_LINE_END = set("「『（〔［｛")
# Characters after which a line may break, preferred over any word boundary. A closing
# mark may end a line, but never start one (see NO_LINE_START).
BREAK_AFTER = set("、。，．！？!? 　」』】…")
# Particles that may start a line after kana or Latin, and after kanji only when main.
PARTICLES = set("はがをにでとのへもやかねよ")
MAIN_PARTICLES = set("はがをにでとのへも")
# A preferred break must leave a first line at least this share of the width.
MIN_FILL = 0.3
# A pause this long ends a caption. Shorter captions are joined to a neighbour across
# shorter pauses; no caption is shown for less than MIN_CAPTION_SECONDS when avoidable.
PAUSE_SECONDS = 0.7
JOIN_PAUSE_SECONDS = (
    0.7  # longest silence across which a short caption may join its neighbour
)
MIN_CAPTION_SECONDS = 1.0
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
_PARSER = None


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
            text = normalize_terms(safe(word.text))
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


# A number with its unit ("1,234万円", "30%") stays on one line when it fits.
NUMBER = re.compile(
    r"\d[\d,.]*(?:[%％]|[年月日時分秒人円万億兆個件回本枚倍名歳点]{1,2})?"
)


# Latin words, versions and URLs stay on one line when they fit ("GPT-4o", "v1.2").
LATIN = re.compile(r"[A-Za-z0-9]+(?:[._:/+%-][A-Za-z0-9]+)*")
URL = re.compile(r"https?://\S+")


def _unbreakable(text):
    """Positions that may not start a line because they are inside a number, term or URL."""
    blocked = set()
    for pattern in (NUMBER, LATIN, URL):
        for match in pattern.finditer(text):
            blocked.update(range(match.start() + 1, match.end()))
    return blocked


def _is_combining(char):
    """A combining mark belongs to the character before it and never starts a line."""
    return unicodedata.category(char) in ("Mn", "Mc", "Me")


def _is_kanji(char):
    return "一" <= char <= "鿿" or char in "々〆ヶ"


def _is_hiragana(char):
    return "぀" <= char <= "ゟ"


def _is_katakana(char):
    return "゠" <= char <= "ヿ"


def _kind(char):
    """Script of one character: h hiragana, k katakana, K kanji, a Latin or digit, o other."""
    if _is_hiragana(char):
        return "h"
    if _is_katakana(char):
        return "k"
    if _is_kanji(char):
        return "K"
    if char.isascii() and char.isalnum():
        return "a"
    return "o"


def _boundary_rank(prev, nxt):
    """How well text may break between two characters.

    3: after a space, sentence punctuation or a closing mark. 2: a word boundary: a particle
    after kana or Latin, or after kanji only for the main particles; a change from hiragana to
    katakana, kanji or Latin; a change between katakana, kanji and Latin.
    1: between two hiragana that are not particles, used only when nothing better fits.
    0: inside a word (kanji with kanji or okurigana, a katakana or Latin run), or not allowed.
    """
    if not prev or not nxt or nxt in NO_LINE_START or prev in NO_LINE_END:
        return 0
    if _is_combining(nxt):
        return 0
    if prev in BREAK_AFTER:
        return 3
    kind_prev, kind_next = _kind(prev), _kind(nxt)
    if nxt in PARTICLES and (
        kind_prev in "hka" or (kind_prev == "K" and nxt in MAIN_PARTICLES)
    ):
        return 2
    if kind_prev == "h" and kind_next in "kKa":
        return 2
    if kind_prev != kind_next and kind_prev in "kaK" and kind_next in "kaK":
        return 2
    if kind_prev == "h" and kind_next == "h":
        return 1
    return 0


def inside_word(prev, nxt):
    """True when a break between prev and nxt would split a word.

    That is a kanji compound, a katakana or Latin run, or kanji followed by okurigana
    (hiragana other than a main particle). Used by the quality check, which reports it.
    """
    kind_prev, kind_next = _kind(prev), _kind(nxt)
    if kind_prev == kind_next and kind_prev in "kaK":
        return True
    return kind_prev == "K" and kind_next == "h" and nxt not in MAIN_PARTICLES


def wrap_breaks(text, measure, line_px):
    """Character indices where a new line starts, at the strongest break that fits.

    A phrase edge from BudouX ranks highest, then the boundaries of _boundary_rank. A break
    inside a word is used only for a word wider than a whole line.
    """
    phrase_starts = set()
    position = 0
    for phrase in parser().parse(text):
        if position:
            phrase_starts.add(position)
        position += len(phrase)
    blocked = _unbreakable(text)
    breaks, begin, used = [], 0, 0.0
    best = {3: None, 2: None, 1: None}
    for index, char in enumerate(text):
        size = measure.width(char)
        if index > begin:
            rank = _boundary_rank(text[index - 1], char)
            if rank and index in phrase_starts:
                rank = 3
            if rank:
                best[rank] = index
        if used + size > line_px and index > begin:
            cut = next(
                (
                    best[rank]
                    for rank in (3, 2, 1)
                    if best[rank] is not None
                    and measure.width(text[begin : best[rank]]) >= MIN_FILL * line_px
                ),
                None,
            )
            if cut is None:
                cut = index
                while cut > begin + 1 and (
                    text[cut] in NO_LINE_START or _is_combining(text[cut])
                ):
                    cut -= 1
            # Move the break in front of a number; a number wider than a line is split.
            start = cut
            while start in blocked and start > begin:
                start -= 1
            if start > begin:
                cut = start
            breaks.append(cut)
            begin = cut
            used = measure.width(text[cut:index])
            # Candidates beyond the new break still apply to the new line.
            best = {
                r: (c if c is not None and c > cut else None) for r, c in best.items()
            }
        used += size
    return breaks


def _word_cut(members):
    """Where a caption may end inside members without splitting a word, or None.

    A head that lasts MIN_CAPTION_SECONDS is preferred, so no caption is a fragment; then a
    word start over a boundary between kana; then the latest boundary.
    """
    best = None
    for k in range(1, len(members)):
        prev, nxt = members[k - 1][2][-1:], members[k][2][:1]
        rank = _boundary_rank(prev, nxt)
        if rank < 1:
            continue
        long_enough = members[k - 1][1] - members[0][0] >= MIN_CAPTION_SECONDS
        key = (long_enough, rank, k)
        if best is None or key > best[0]:
            best = (key, k)
    return best[1] if best else None


# Polite sentence endings. Recognition writes no full stop, so one of these ends a sentence.
POLITE_ENDING = re.compile(r"(です|ます|でした|ました|ません|ませんでした)$")


def _sentence_done(members):
    """True when members end a sentence and have lasted MIN_CAPTION_SECONDS, enough for one caption."""
    long_enough = members[-1][1] - members[0][0] >= MIN_CAPTION_SECONDS
    return bool(POLITE_ENDING.search(members[-1][2])) and long_enough


def group(tokens, measure, line_px, max_lines=2):
    """Caption groups that fit in max_lines.

    A pause ends a group. When the text overflows, the group ends at the last word boundary
    before the overflow, so no word is split between two captions; the rest carries over.
    """

    def overflows(members, token):
        joined = "".join(item[2] for item in members) + token[2]
        too_many_lines = len(wrap_breaks(joined, measure, line_px)) + 1 > max_lines
        return too_many_lines or token[1] - members[0][0] > 4

    groups, current = [], []
    for token in tokens:
        if current and token[0] - current[-1][1] > PAUSE_SECONDS:
            groups.append(current)
            current = []
        elif current and _sentence_done(current):
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
    return "".join(out)


def spoken_length(text):
    """Characters a viewer reads; spaces and line breaks do not count."""
    return len(re.sub(r"\s", "", text))


def hold_for_reading(spans, texts, limit):
    """Extend captions read faster than the target into the silence after them.

    spans holds [start, end] in milliseconds, in order, and is changed in place. Only
    ends move, and never past the next caption's start or the clip's end, so no caption
    starts later than its speech or overlaps another.
    """
    for index, (span, text) in enumerate(zip(spans, texts)):
        ceiling = spans[index + 1][0] if index + 1 < len(spans) else max(limit, span[1])
        wanted = span[0] + math.ceil(spoken_length(text) * 1000 / TARGET_CAPTION_CPS)
        span[1] = max(span[1], min(wanted, ceiling))
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


def create(transcript, clip, directory, settings, keep=None, credits=None):
    height = settings.height
    margin = margin_px(height)
    numerator, denominator = map(int, settings.aspect.split(":"))
    frame_width = round(height * numerator / denominator / 2) * 2
    font_size = round(height * 0.048)
    font_path = fonts.resolve(settings.font)
    measure = fonts.Measure(font_path, font_size)
    line_px = frame_width - 2 * margin - round(OUTLINE_ALLOWANCE * scale(height))
    duration = sum(b - a for a, b in keep) if keep else clip.end - clip.start

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
        marginl=margin,
        marginr=margin,
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
            marginl=margin,
            marginr=margin,
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
            marginr=margin,
            marginv=round(height * 0.015),
        )
        credit_text = safe(" / ".join(credits))
        breaks = wrap_breaks(
            credit_text, fonts.Measure(font_path, credit_size), frame_width - 2 * margin
        )
        subtitles.append(
            pysubs2.SSAEvent(
                start=0,
                end=round(duration * 1000),
                style="Credit",
                text=with_breaks(credit_text, breaks),
            )
        )

    style = "Sequential" if settings.caption == "sequential" else "Default"
    plain_lines = ["".join(item[2] for item in items) for items in groups]
    spans, previous_end = [], 0
    for items in groups:
        start = max(previous_end, round(items[0][0] * 1000))
        # A group squeezed out by the previous one is kept for a moment, never dropped.
        end = max(round(items[-1][1] * 1000), start + MIN_EVENT_MS)
        spans.append([start, end])
        previous_end = end
    hold_for_reading(spans, plain_lines, round(duration * 1000))
    for items, (start, end) in zip(groups, spans):
        text = group_text(items, measure, line_px, settings.caption)
        subtitles.append(pysubs2.SSAEvent(start=start, end=end, style=style, text=text))

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
    }
