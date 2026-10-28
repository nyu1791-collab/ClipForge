"""Prepare a private, offline human review package from saved editorial data."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

from .audio_first import retrieved
from .media_sources import canonical, open_source
from .models import Settings
from .storage import PipelineError
from .release_transfer import REPO, gh, private_repo


def normalized(text):
    return re.sub(r"[\s、。]", "", text)


def clock(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def review_rows(clip, transcript, raw):
    """Flag possible issues, without asserting that a flag is an error."""
    source = raw.get("segments", [])
    turns = clip.get("speaker_turns", [])
    result = []
    previous_speaker = None
    for number, segment in enumerate(transcript.get("segments", []), 1):
        start, end = segment["start"], segment["end"]
        origins = [s for s in source if s["start"] < end and s["end"] > start]
        original = "".join(s["text"] for s in origins)
        speakers = {t.get("speaker_id") for t in turns if t["start"] < end and t["end"] > start}
        reasons = []
        if not origins:
            reasons.append("元の文字起こしとの対応を確認")
        elif normalized(original) != normalized(segment["text"]):
            reasons.append("清書で語が変わった可能性")
        if original.count("?") + original.count("？") != segment["text"].count("?") + segment["text"].count("？"):
            reasons.append("疑問符の変更")
        if not speakers or None in speakers:
            reasons.append("話者不明・重複発話の可能性")
        if any(s and s != "speaker_1" for s in speakers):
            reasons.append("別の話者の可能性")
        if previous_speaker is not None and speakers != previous_speaker:
            reasons.append("話者交代の可能性")
        if re.search(r"[0-9０-９〇零一二三四五六七八九十百千万億兆]+", segment["text"]):
            reasons.append("数字・数量の可能性")
        if any(word in segment["text"] for word in ("絶対", "確実", "必ず", "だけ", "100%", "１００％")):
            reasons.append("断定・限定の表現")
        previous_speaker = speakers
        result.append({"number": number, "start": start, "end": end,
                       "text": segment["text"], "original": original,
                       "speakers": sorted(s or "unknown" for s in speakers), "reasons": reasons})
    return result


def make_page(selection, transcripts, raws):
    esc = lambda value: html.escape(str(value), quote=True)
    parts = ["""<!doctype html><html lang="ja"><meta charset="utf-8"><title>ClipForge 確認用</title>
<style>body{font-family:sans-serif;max-width:1100px;margin:auto;padding:1em;line-height:1.6}
video{width:100%;max-width:800px;background:#000}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ccc;text-align:left;vertical-align:top;padding:.5em}
.flag{background:#fff3d3}button{cursor:pointer}</style>
<h1>話題別の確認用動画と字幕</h1><p>字幕を焼き込む前の確認用動画です。時刻は動画の先頭からの目安です。
候補は機械的な目印で、網羅や正確さを保証しません。原音・映像と照合してください。
人名・店名などの固有名詞、見出し、話題の切れ目、各動画の冒頭と末尾も確認してください。
番号と時刻、修正内容を会話でお知らせください。</p>"""]
    for index, (clip, transcript, raw) in enumerate(zip(selection["clips"], transcripts["clips"], raws), 1):
        rows = review_rows(clip, transcript, raw)
        parts.append(f'<section><h2>{index}. {esc(clip["title"])}</h2>')
        parts.append(f'<p>元配信 {clock(clip["start"])}〜{clock(clip["end"])}　見出しと内容を確認</p>')
        parts.append(f'<video id="v{index}" controls preload="metadata" src="clip-{index:03}.mp4"></video>')
        parts.append('<table><thead><tr><th>番号・時刻</th><th>字幕</th><th>確認候補</th></tr></thead><tbody>')
        for row in rows:
            reasons = "、".join(row["reasons"])
            original = (f'<br><small>文字起こし: {esc(row["original"])}</small>'
                        if row["original"] and normalized(row["original"]) != normalized(row["text"]) else "")
            parts.append(f'<tr class="{"flag" if reasons else ""}"><td>{row["number"]} '
                         f'<button type="button" onclick="seek({index},{float(row["start"]):.3f})">{clock(row["start"])}</button></td>'
                         f'<td>{esc(row["text"])}{original}</td><td>{esc(reasons)} '
                         f'<small>{esc(", ".join(row["speakers"]))}</small></td></tr>')
        parts.append('</tbody></table>')
        if not rows:
            parts.append('<p>時刻付き字幕がありません。動画の音声を確認してください。</p>')
        missing = [source for source in raw.get("segments", [])
                   if not any(source["start"] < segment["end"] and source["end"] > segment["start"]
                              for segment in transcript.get("segments", []))]
        if missing:
            parts.append('<h3>字幕への対応が見つからなかった文字起こし</h3><ul>')
            for source in missing:
                parts.append(f'<li><button type="button" onclick="seek({index},{float(source["start"]):.3f})">'
                             f'{clock(source["start"])}</button> {esc(source["text"])}</li>')
            parts.append('</ul>')
        if raw.get("untimed"):
            parts.append('<p>時刻を特定できなかった文字起こし（音声で確認）: ' +
                         esc(" / ".join(str(row.get("text", "")) for row in raw["untimed"])) + '</p>')
        parts.append('</section>')
    parts.append('<script>function seek(i,t){const v=document.getElementById("v"+i);v.currentTime=t;v.play();v.scrollIntoView({behavior:"smooth",block:"center"})}</script></html>')
    return "\n".join(parts)


def create(editorial, output, config):
    editorial, output = Path(editorial), Path(output)
    if output.exists():
        raise PipelineError("Review package already exists")
    selection = json.loads((editorial / "final-selection.json").read_text())
    transcripts = json.loads((editorial / "selected-transcripts.json").read_text())
    clips = selection.get("clips", [])
    if selection.get("selection_basis") != "audio_understanding" or not clips or len(clips) != len(transcripts.get("clips", [])):
        raise PipelineError("Saved editorial selection and transcripts do not match")
    raws = [json.loads((editorial / f"transcribe-selected-{i:03}.json").read_text()) for i in range(1, len(clips) + 1)]
    settings = Settings.model_validate(json.loads(Path(config).read_text()))
    with tempfile.TemporaryDirectory(prefix="clipforge-review-") as tmp:
        root = Path(tmp)
        vod = open_source(canonical(selection["source_url"]), root / "cache",
                          max_bytes=settings.max_transport_bytes, max_seconds=settings.max_source_seconds)
        try:
            for i, clip in enumerate(clips, 1):
                retrieved(vod, root / f"clip-{i:03}.mp4", clip["start"], clip["end"], quality="preview")
        finally:
            close = getattr(vod, "close", None)
            if close:
                close()
        (root / "review.html").write_text(make_page(selection, transcripts, raws), encoding="utf-8")
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            bundle.write(root / "review.html", "review.html")
            for i in range(1, len(clips) + 1):
                bundle.write(root / f"clip-{i:03}.mp4", f"clip-{i:03}.mp4")


def collect_package(run_id, output, state, save):
    """Read the private asset through REST; never dispatch or replace a saved file."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise PipelineError("Invalid review run")
    private_repo()
    tag = f"clipforge-kick-editorial-{run_id}"
    release = json.loads(gh("api", f"repos/{REPO}/releases/tags/{tag}"))
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == "review-package.zip"]
    if len(assets) != 1 or type(assets[0].get("size")) is not int or assets[0]["size"] <= 0:
        raise PipelineError("Private review package is missing or ambiguous")
    asset = assets[0]
    if type(asset.get("id")) is not int or asset["id"] <= 0:
        raise PipelineError("Invalid review package asset")
    destination = Path(output) / "review-package.zip"
    saved = state.get("review_package", {})
    if destination.exists():
        if (not destination.is_file() or destination.is_symlink() or
            saved.get("asset_id") != asset["id"] or saved.get("size") != destination.stat().st_size or
            saved.get("size") != asset["size"]):
            raise PipelineError("Existing review package has no matching saved identity")
        digest = hashlib.sha256()
        with destination.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if saved.get("sha256") != digest.hexdigest():
            raise PipelineError("Existing review package has changed")
        return destination
    fd, temporary_name = tempfile.mkstemp(prefix=".review-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as out:
            result = subprocess.run(["gh", "api", f"repos/{REPO}/releases/assets/{asset['id']}",
                                     "-H", "Accept: application/octet-stream"],
                                    stdout=out, stderr=subprocess.PIPE, timeout=1800)
        if result.returncode or temporary.stat().st_size != asset["size"]:
            raise PipelineError("Private review download failed or has the wrong size")
        digest = hashlib.sha256()
        with temporary.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        asset_digest = asset.get("digest")
        if asset_digest and asset_digest != "sha256:" + digest.hexdigest():
            raise PipelineError("Private review package digest mismatch")
        state["review_package"] = {"asset_id": asset["id"], "size": asset["size"], "sha256": digest.hexdigest()}
        save()
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editorial", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    create(args.editorial, args.output, args.config)


if __name__ == "__main__":
    main()
