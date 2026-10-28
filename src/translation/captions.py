"""Build cues from source word times and retain them during translation."""

import re
import budoux
import pysubs2

_japanese_parser = budoux.load_default_japanese_parser()


def append_word(text, word):
    if not text:
        return word.strip()
    if word[:1].isspace() or text[-1:].isspace():
        return text + word
    if re.search(r"[A-Za-z0-9]$", text) and re.match(r"[A-Za-z0-9]", word):
        return text + " " + word
    return text + word


def make_cues(words, chunk_index):
    result = []
    for word in sorted(words, key=lambda w: (w["start"], w["end"])):
        speaker = word.get("source_speaker")
        split = (
            not result
            or result[-1]["speaker"] != speaker
            or word["start"] - result[-1]["end"] > 0.9
            or word["end"] - result[-1]["start"] > 7.0
            or (
                re.search(r"[.!?。！？]$", result[-1]["text"])
                and word["start"] - result[-1]["end"] > 0.15
            )
        )
        if split:
            result.append(
                {
                    "id": f"{chunk_index}-{len(result) + 1}",
                    "start": word["start"],
                    "end": word["end"],
                    "text": word["text"].strip(),
                    "speaker": speaker,
                    "chunk": chunk_index,
                }
            )
        else:
            result[-1]["end"] = max(result[-1]["end"], word["end"])
            result[-1]["text"] = append_word(result[-1]["text"], word["text"])
    return result


def apply_translation(cues, response):
    """Unknown, missing or duplicate IDs retain source text and are reported."""
    found, duplicates = {}, set()
    items = response.get("segments", []) if isinstance(response, dict) else []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            key = item["id"]
            if key in found:
                duplicates.add(key)
            elif isinstance(item.get("text"), str) and item["text"].strip():
                found[key] = item["text"].strip()
    missing = []
    out = []
    for cue in cues:
        text = found.get(cue["id"]) if cue["id"] not in duplicates else None
        if not text:
            missing.append(cue["id"])
        out.append(
            {**cue, "translation": text or cue["text"], "translated": bool(text)}
        )
    return out, missing


def wrap_text(text, limit):
    safe = text.replace("\\", "＼").replace("{", "（").replace("}", "）")
    lines = []
    for paragraph in safe.splitlines() or [""]:
        line = ""
        for phrase in _japanese_parser.parse(paragraph):
            if line and len(line) + len(phrase) > limit:
                lines.append(line)
                line = ""
            line += phrase
        if line:
            lines.append(line)
    return "\\N".join(lines)


def write_subtitles(
    cues, directory, bilingual=False, font="Noto Sans CJK JP", size=24, line_chars=25
):
    directory.mkdir(parents=True, exist_ok=True)
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = "640"
    subs.info["PlayResY"] = "360"
    subs.styles["Default"] = pysubs2.SSAStyle(
        fontname=font,
        fontsize=size,
        primarycolor=pysubs2.Color(255, 255, 255),
        outlinecolor=pysubs2.Color(0, 0, 0),
        outline=2,
        shadow=0,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginv=24,
    )
    for cue in cues:
        text = wrap_text(cue["translation"], line_chars)
        if bilingual and cue["translated"]:
            text += "\\N" + wrap_text(cue["text"], line_chars)
        subs.events.append(
            pysubs2.SSAEvent(
                start=round(cue["start"] * 1000),
                end=round(cue["end"] * 1000),
                text=text,
            )
        )
    subs.save(str(directory / "subtitles.ass"))
    subs.save(str(directory / "subtitles.srt"))
