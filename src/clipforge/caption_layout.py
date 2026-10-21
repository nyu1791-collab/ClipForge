"""Local Japanese word/phrase layout; ASR token boundaries are not word boundaries."""

import re
from functools import lru_cache

from .models import Word
from .storage import PipelineError

NO_START = set("、。，．・：；！？!?」』）〕］｝ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶー 　")
NO_END = set("「『（〔［｛")


@lru_cache(maxsize=1)
def engines():
    import budoux
    from janome.tokenizer import Tokenizer

    return Tokenizer(), budoux.load_default_japanese_parser()


def units(text):
    """Keep lexical words, following punctuation, and small kana together."""
    tokenizer, _ = engines()
    result, cursor = [], 0
    for token in tokenizer.tokenize(text, wakati=True):
        start = text.find(token, cursor)
        if start < 0:
            raise PipelineError("Japanese tokenizer did not preserve caption text")
        gap = text[cursor:start]
        cursor = start + len(token)
        if gap and result:
            result[-1] += gap
        elif gap:
            token = gap + token
        if result and (token[0] in NO_START or token.isspace()):
            result[-1] += token
        elif result and result[-1][-1] in NO_END:
            result[-1] += token
        else:
            result.append(token)
    if text[cursor:]:
        if result:
            result[-1] += text[cursor:]
        else:
            result = [text]
    if "".join(result) != text:
        raise PipelineError("caption text reconstruction failed")
    return result


def phrase_edges(text):
    _, parser = engines()
    cursor, edges = 0, set()
    for phrase in parser.parse(text):
        if cursor:
            edges.add(cursor)
        cursor += len(phrase)
    return edges


def line_break(parts, measure, limit):
    """One or two lines, breaking only between lexical units; None means overflow."""
    text = "".join(parts)
    if measure(text) <= limit:
        return []
    phrases = phrase_edges(text)
    cursor, choices = 0, []
    for i, part in enumerate(parts[:-1], 1):
        cursor += len(part)
        left, right = text[:cursor], text[cursor:]
        if not right or right[0] in NO_START or left[-1] in NO_END:
            continue
        a, b = measure(left), measure(right)
        if max(a, b) <= limit:
            rank = 2 if left[-1] in "、。！？!? 　" else int(cursor in phrases)
            choices.append((rank, -abs(a - b), i))
    return [max(choices)[2]] if choices else None


def timed_units(words):
    """Retokenize whole speech runs, preserving ASR timing at each character.

    Only boundaries inside an ASR token need proportional interpolation. Timing is
    approximate there and is counted by create(), never claimed as forced alignment.
    """
    result, run, interpolated = [], [], 0

    def flush():
        nonlocal interpolated
        if not run:
            return
        chars = []
        for w in run:
            for i, char in enumerate(w.text):
                chars.append((char, w.start + (w.end-w.start)*i/len(w.text),
                              w.start + (w.end-w.start)*(i+1)/len(w.text)))
        text = "".join(c[0] for c in chars)
        cursor = 0
        original_edges = set()
        for w in run:
            cursor += len(w.text)
            original_edges.add(cursor)
        cursor = 0
        for part in units(text):
            stop = cursor + len(part)
            result.append(Word(start=chars[cursor][1], end=chars[stop-1][2], text=part))
            interpolated += int(stop < len(chars) and stop not in original_edges)
            cursor = stop
        run.clear()

    for word in words:
        if run and word.start - run[-1].end > 0.7:
            flush()
        run.append(word)
    flush()
    return result, interpolated


def groups(words, measure, limit):
    result, current = [], []

    def fits(items):
        return (items[-1].end - items[0].start <= 4.001 and
                line_break([w.text for w in items], measure, limit) is not None)

    for word in words:
        if current and (word.start - current[-1].end > 0.7 or not fits(current + [word])):
            result.append(current)
            current = []
        if not fits([word]) and not (
            word.end-word.start <= 4.001 and measure(word.text)*0.6 <= limit
        ):
            raise PipelineError("caption word exceeds layout or 4s; review subtitle timing/text")
        current.append(word)
    if current:
        result.append(current)
    index = 0
    while index < len(result):
        if result[index][-1].end - result[index][0].start < 1:
            for other in (index-1, index+1):
                if not 0 <= other < len(result):
                    continue
                a, b = sorted((index, other))
                combined = result[a] + result[b]
                if result[b][0].start - result[a][-1].end <= 0.7 and fits(combined):
                    result[a:b+1] = [combined]
                    index = max(-1, a-1)
                    break
        index += 1
    return result


def normalize(text):
    terms = {"ai": "AI", "agi": "AGI", "asi": "ASI"}
    return re.sub(r"[A-Za-z]+", lambda m: terms.get(m[0].lower(), m[0]), text)
