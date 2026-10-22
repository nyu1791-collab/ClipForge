"""Model-free review hints for audio topic candidates.

Hints only suggest what a reviewer should look at; they never choose clips.
Results carry topic IDs, flags and numbers. Transcript text is read in memory
and is never returned, printed or logged.
"""

import argparse
import json
import math
from pathlib import Path
import re
import sys
import unicodedata

EDGE_START_SECONDS = 120
EDGE_END_SECONDS = 180
GREETING_PHRASES = (
    "おはよう",
    "こんにちは",
    "こんばんは",
    "おやすみ",
    "お疲れ",
    "フォローありがとう",
    "初見さん",
    "また明日",
    "バイバイ",
    "ばいばい",
)
GREETING_MIN_HITS = 2
REPEAT_MIN_COUNT = 10
REPEAT_RE = re.compile(rf"(\S{{1,4}}?)\1{{{REPEAT_MIN_COUNT - 1},}}")
CHARS_PER_SECOND_REFERENCE = 8.0
# Sharing at least half the distinct trigrams is a review cue, not semantic proof.
NEAR_DUPLICATE_THRESHOLD = 0.5
PENALTY = {
    "edge": 0.1,
    "greeting": 0.2,
    "repetition": 0.5,
    "profanity": 0.5,
    "duplicate": 0.2,
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _topic(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
        raise ValueError("invalid topic row")
    start, end, speech = (
        _number(row.get("start")),
        _number(row.get("end")),
        _number(row.get("speech_seconds")),
    )
    text = row.get("text")
    if start is None or end is None or speech is None or not isinstance(text, str):
        raise ValueError("invalid topic row")
    return {
        "id": row["id"],
        "start": start,
        "end": end,
        "speech": speech,
        "text": text,
    }


def _stream_duration(analysis, topics):
    duration = _number(analysis.get("duration"))
    if duration is not None and duration > 0:
        return duration
    return max((topic["end"] for topic in topics), default=0.0)


def _terms(profanity_terms):
    terms = set()
    for term in profanity_terms:
        if not isinstance(term, str):
            raise ValueError("profanity terms must be strings")
        if term.strip():
            terms.add(term.strip())
    return frozenset(terms)


def _repeat_max(text):
    """Longest run of one 1-4 character unit, counted in repetitions."""
    best = 0
    for match in REPEAT_RE.finditer(text):
        best = max(best, len(match.group(0)) // len(match.group(1)))
    return best


def _speech_ratio(topic):
    length = topic["end"] - topic["start"]
    return min(1.0, topic["speech"] / length) if length > 0 else 0.0


def _trigrams(text):
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if not char.isspace() and unicodedata.category(char)[0] not in "PS"
    )
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _mark_near_duplicates(topics, hints):
    grams = [_trigrams(topic["text"]) for topic in topics]
    densities = [_speech_ratio(topic) for topic in topics]
    matches = [[] for _ in topics]
    maxima = [0.0 for _ in topics]
    duplicates = set()
    for left in range(len(topics)):
        for right in range(left + 1, len(topics)):
            union = grams[left] | grams[right]
            similarity = len(grams[left] & grams[right]) / len(union) if union else 0.0
            maxima[left] = max(maxima[left], similarity)
            maxima[right] = max(maxima[right], similarity)
            if similarity >= NEAR_DUPLICATE_THRESHOLD:
                matches[left].append((right, similarity))
                matches[right].append((left, similarity))
                duplicates.add(
                    min(
                        (left, right),
                        key=lambda index: (
                            densities[index],
                            -topics[index]["start"],
                            -index,
                        ),
                    )
                )
    for index, hint in enumerate(hints):
        matches[index].sort(
            key=lambda match: (-match[1], topics[match[0]]["start"], match[0])
        )
        hint["near_duplicates"] = [
            topics[other]["id"] for other, _ in matches[index][:3]
        ]
        hint["max_similarity"] = round(maxima[index], 3)
        hint["duplicate"] = index in duplicates
        if hint["duplicate"]:
            hint["score"] = round(hint["score"] - PENALTY["duplicate"], 4)


def _hint(topic, duration, terms):
    text = topic["text"]
    start, end = topic["start"], topic["end"]
    length = end - start
    edge = start <= EDGE_START_SECONDS or end >= duration - EDGE_END_SECONDS
    greeting_hits = sum(text.count(phrase) for phrase in GREETING_PHRASES)
    greeting = greeting_hits >= GREETING_MIN_HITS
    repeat_max = _repeat_max(text)
    repetition = repeat_max >= REPEAT_MIN_COUNT
    profanity = None
    if terms is not None:
        profanity = any(term in text for term in terms)
    speech_ratio = _speech_ratio(topic)
    if length > 0:
        chars = len(re.sub(r"\s", "", text))
        chars_per_second = chars / length
    else:
        chars_per_second = 0.0
    base = 0.5 * speech_ratio + 0.5 * min(
        1.0, chars_per_second / CHARS_PER_SECOND_REFERENCE
    )
    flags = {
        "edge": edge,
        "greeting": greeting,
        "repetition": repetition,
        "profanity": profanity is True,
    }
    penalty = sum(PENALTY[name] for name, flagged in flags.items() if flagged)
    return {
        "id": topic["id"],
        "start": start,
        "end": end,
        "edge": edge,
        "greeting": greeting,
        "greeting_hits": greeting_hits,
        "repetition": repetition,
        "repeat_max": repeat_max,
        "profanity": profanity,
        "speech_ratio": round(speech_ratio, 3),
        "chars_per_second": round(chars_per_second, 2),
        "score": round(base - penalty, 4),
    }


def hint_topics(analysis, *, profanity_terms=None):
    """Return flags and numbers per topic, highest score first.

    ``profanity_terms`` is an optional iterable of terms supplied by the caller.
    When omitted, the profanity flag is ``None`` (not judged).
    """
    rows = analysis.get("topics") if isinstance(analysis, dict) else None
    if not isinstance(rows, list):
        raise ValueError("topics list is required")
    terms = None if profanity_terms is None else _terms(profanity_terms)
    topics = [_topic(row) for row in rows]
    duration = _stream_duration(analysis, topics)
    hints = [_hint(topic, duration, terms) for topic in topics]
    _mark_near_duplicates(topics, hints)
    hints.sort(key=lambda hint: (-hint["score"], hint["start"]))
    return {
        "status": "topic_hints",
        "topics": hints,
        "model_generation_requests": 0,
    }


def _read_terms(path):
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topics_json", type=Path)
    parser.add_argument(
        "--profanity-dict",
        type=Path,
        help="UTF-8 file with one term per line; omitted means not judged",
    )
    args = parser.parse_args(argv)
    try:
        analysis = json.loads(args.topics_json.read_text(encoding="utf-8"))
        terms = _read_terms(args.profanity_dict) if args.profanity_dict else None
        result = hint_topics(analysis, profanity_terms=terms)
    except (OSError, ValueError, KeyError, TypeError):
        print("topic hints failed: invalid input", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
