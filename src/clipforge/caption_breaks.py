"""Line-break rules for Japanese captions: script kinds, break ranks and wrap points."""

import re
import unicodedata


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
# No caption is shown for less than this when avoidable (see _word_cut and captions._join_short).
MIN_CAPTION_SECONDS = 1.0

# Japanese phrase parser (BudouX), loaded on first use.
_PARSER = None


def parser():
    """Phrase boundaries for Japanese text (BudouX, no network or model download)."""
    global _PARSER
    if _PARSER is None:
        import budoux

        _PARSER = budoux.load_default_japanese_parser()
    return _PARSER


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
    if len(breaks) == 1:
        breaks = [
            _balanced_break(text, measure, line_px, blocked, phrase_starts) or breaks[0]
        ]
    return breaks


def _balanced_break(text, measure, line_px, blocked, phrase_starts):
    """The two-line break whose lines are closest in width, at a word edge.

    A greedy fill leaves the second line short; a balanced pair reads better. Phrase edges from
    BudouX are preferred; a rank-2 edge is used only when no phrase edge fits. Both lines must
    still fit, and a line never starts with a particle. None when no such break exists.
    """
    for phrases_only in (True, False):
        best = None
        for index in range(1, len(text)):
            if index in blocked or text[index] in PARTICLES:
                continue
            rank = _boundary_rank(text[index - 1], text[index])
            if phrases_only and (index not in phrase_starts or not rank):
                continue
            if not phrases_only and rank < 2:
                continue
            first, second = measure.width(text[:index]), measure.width(text[index:])
            if first > line_px or second > line_px:
                continue
            if best is None or abs(first - second) < best[0]:
                best = (abs(first - second), index)
        if best is not None:
            return best[1]
    return None


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
