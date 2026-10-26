"""Word-aligned caption edits, with unchanged ASR words left on their original clock."""

from difflib import SequenceMatcher

from .storage import PipelineError


def minimal_edits(segment, corrected, at):
    before = segment.text
    if before == corrected:
        return []
    words = segment.words
    if not words or "".join(w.text for w in words) != before:
        raise PipelineError("caption changes require aligned ASR words")
    boundaries, position = [0], 0
    for word in words:
        position += len(word.text)
        boundaries.append(position)
    operations = SequenceMatcher(None, before, corrected, autojunk=False).get_opcodes()
    ranges = []
    for tag, a, b, _, _ in operations:
        if tag == "equal":
            continue
        # Expand changed text to whole ASR words, including insertions at an edge.
        left = max(x for x in boundaries[:-1] if x <= min(a, len(before) - 1))
        right = min(x for x in boundaries[1:] if x >= max(b, left + 1))
        if ranges and left <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], right)
        else:
            ranges.append([left, right])

    def mapped(boundary, side):
        choices = []
        for tag, a, b, c, d in operations:
            if a <= boundary <= b:
                if tag == "equal":
                    choices.append(c + boundary - a)
                elif a == b:
                    choices.extend([c, d])
                elif boundary == a:
                    choices.append(c)
                elif boundary == b:
                    choices.append(d)
                else:
                    choices.extend([c, d])
        return (min if side == "left" else max)(choices)

    edits = []
    for left, right in ranges:
        source = before[left:right]
        if before.count(source) != 1 or len(source) > 100:
            raise PipelineError("caption change is ambiguous or exceeds the edit bound")
        replacement = corrected[mapped(left, "left") : mapped(right, "right")]
        if len(replacement) > 100:
            raise PipelineError("caption replacement exceeds the edit bound")
        edits.append({"at": at, "from": source, "to": replacement})
    return edits
