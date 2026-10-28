"""Preserve word times only when edited lexical characters are unchanged."""

import unicodedata


def _content(char):
    return not char.isspace() and not unicodedata.category(char).startswith(("P", "Z"))


def preserve_word_timing(words, edited):
    """Map punctuation-only edits to source words; never realign rewritten speech."""
    if not words or not isinstance(edited, str):
        return []
    if not all(
        isinstance(w, dict) and isinstance(w.get("text"), str) and w["text"]
        for w in words
    ):
        return []
    original = "".join(w["text"] for w in words)
    if original == edited:
        return [dict(w) for w in words]
    source_content = "".join(c for c in original if _content(c))
    positions = [i for i, c in enumerate(edited) if _content(c)]
    if not positions or "".join(edited[i] for i in positions) != source_content:
        return []
    counts = [sum(_content(c) for c in w["text"]) for w in words]
    if any(count == 0 for count in counts):
        return []
    result, consumed, previous = [], 0, 0
    for word, count in zip(words, counts):
        consumed += count
        end = positions[consumed] if consumed < len(positions) else len(edited)
        text = edited[previous:end]
        if not text:
            return []
        result.append({**word, "text": text})
        previous = end
    return result if "".join(word["text"] for word in result) == edited else []
