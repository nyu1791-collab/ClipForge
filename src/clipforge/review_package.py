"""Prepare a private, offline human review package from saved editorial data."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import random
import re
import stat
import subprocess
import tempfile
import zipfile

from .audio_first import retrieved
from .media_sources import canonical, open_source
from .models import Settings
from .storage import PipelineError, fingerprint
from .release_transfer import MAX_BYTES, REPO, gh, private_repo


def normalized(text):
    return re.sub(r"[\s、。]", "", text)


def clock(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def group_candidates(rows):
    groups = []
    for row in rows:
        if not row["reasons"]:
            continue
        if groups and row["start"] <= groups[-1][-1]["end"] + 1.5:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def focused_groups(rows):
    """Group nearby uncertain speaker assignments without flagging every second voice."""
    groups = []
    for row in rows:
        important = (
            len(row["speakers"]) > 1
            or any(
                key in reason
                for reason in row["reasons"]
                for key in ("話者交代", "話者不明", "元IDの対応不明")
            )
        )
        if not important:
            continue
        if (
            groups
            and row["start"] <= groups[-1][-1]["end"] + 6
            and row["end"] - groups[-1][0]["start"] <= 45
        ):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def inspect_archive(path, source_url):
    """Check the private package identity and safe, expected member names."""
    with zipfile.ZipFile(path) as bundle:
        info = bundle.infolist()
        names = [item.filename for item in info]
        if (
            len(names) != len(set(names))
            or sum(item.file_size for item in info) > MAX_BYTES
        ):
            raise PipelineError("Invalid review package contents")
        result, documents = {}, {}
        for name in ("final-selection.json", "selected-transcripts.json"):
            matches = [item for item in info if item.filename == name]
            if len(matches) != 1 or matches[0].file_size > 8 * 1024**2:
                raise PipelineError(
                    "The review package lacks its original editorial data"
                )
            data = bundle.read(matches[0])
            result[name] = hashlib.sha256(data).hexdigest()
            documents[name] = json.loads(data)
        selection = documents["final-selection.json"]
        transcripts = documents["selected-transcripts.json"]
        clips = selection.get("clips", [])
        if (
            selection.get("selection_basis") != "audio_understanding"
            or canonical(selection.get("source_url")) != canonical(source_url)
            or not isinstance(clips, list)
            or not 1 <= len(clips) <= 10
            or len(transcripts.get("clips", [])) != len(clips)
        ):
            raise PipelineError("Review package belongs to another source or selection")
        expected = {"review.html", "final-selection.json", "selected-transcripts.json"}
        expected.update(f"clip-{i:03}.mp4" for i in range(1, len(clips) + 1))
        if set(names) != expected or any(
            item.is_dir()
            or item.file_size == 0
            or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}
            for item in info
        ):
            raise PipelineError("Invalid review package contents")
        return result


def validate_local_package(state, state_path):
    saved = state.get("review_package", {})
    path = Path(state_path).parent / "review-package.zip"
    if (
        path.is_symlink()
        or not path.is_file()
        or type(saved.get("size")) is not int
        or path.stat().st_size != saved["size"]
    ):
        raise PipelineError("The reviewed package is missing or has changed")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if saved.get("sha256") != digest.hexdigest():
        raise PipelineError("The reviewed package is missing or has changed")
    return inspect_archive(path, state.get("source_url"))


def review_rows(clip, transcript, raw, provenance=None):
    """Flag possible issues, without asserting that a flag is an error."""
    source = raw.get("segments", [])
    turns = clip.get("speaker_turns", [])
    result = []
    previous_speaker = None
    for number, segment in enumerate(transcript.get("segments", []), 1):
        start, end = segment["start"], segment["end"]
        members = [
            row
            for row in (provenance or {}).get("segments", [])
            if row["start"] >= start - 0.001 and row["end"] <= end + 0.001
        ]
        members.sort(key=lambda row: (row["start"], row["end"]))
        ids = [i for member in members for i in member.get("source_ids", [])]
        exact = (
            bool(ids)
            and all(type(i) is int and 1 <= i <= len(source) for i in ids)
            and len(ids) == len(set(ids))
            and "\n".join(row["text"] for row in members) == segment["text"]
        )
        origins = [source[i - 1] for i in ids] if exact else []
        original = "".join(s["text"] for s in origins)
        speakers = {
            t.get("speaker_id") for t in turns if t["start"] < end and t["end"] > start
        }
        reasons = []
        if not exact:
            reasons.append("元IDの対応不明：文字起こしと音声を確認")
        elif normalized(original) != normalized(segment["text"]):
            reasons.append("清書で語が変わった可能性")
        if original.count("?") + original.count("？") != segment["text"].count(
            "?"
        ) + segment["text"].count("？"):
            reasons.append("疑問符の変更")
        if not speakers or None in speakers:
            reasons.append("話者不明・重複発話の可能性")
        if any(s and s != "speaker_1" for s in speakers):
            reasons.append("別の話者の可能性")
        if previous_speaker is not None and speakers != previous_speaker:
            reasons.append("話者交代の可能性")
        if re.search(
            r"[0-9０-９]|[〇零一二三四五六七八九十百千万億兆]+[年月日円人個回時分秒歳度本枚万億兆]",
            segment["text"],
        ):
            reasons.append("数字・数量の可能性")
        if any(
            word in segment["text"]
            for word in ("絶対", "確実", "必ず", "だけ", "100%", "１００％")
        ):
            reasons.append("断定・限定の表現")
        boundary = 896.0
        while boundary < clip["end"] - clip["start"]:
            if start < boundary + 2 and end > boundary - 2:
                reasons.append("文字起こしの窓の境界付近")
                break
            boundary += 896.0
        previous_speaker = speakers
        result.append(
            {
                "number": number,
                "start": start,
                "end": end,
                "text": segment["text"],
                "original": original,
                "speakers": sorted(s or "unknown" for s in speakers),
                "reasons": reasons,
                "source_ids": ids if exact else [],
                "mapping_exact": exact,
            }
        )
    return result


def make_page(selection, transcripts, raws, provenances):
    def esc(value):
        return html.escape(str(value), quote=True)

    parts = [
        """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ClipForge 確認・字幕編集</title>
<style>body{font-family:sans-serif;max-width:1100px;margin:auto;padding:1em;line-height:1.6}
video{width:100%;max-width:800px;background:#000}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ccc;text-align:left;vertical-align:top;padding:.5em}
.flag{background:#fff3d3}button{cursor:pointer;padding:.45em} .viewer{position:relative;max-width:800px}
.overlay{position:absolute;bottom:12%;left:4%;right:4%;text-align:center;color:white;font-weight:bold;text-shadow:1px 1px 3px black,-1px -1px 3px black;white-space:pre-wrap;pointer-events:none}
.modal{position:fixed;inset:0;background:#0009;z-index:10;overflow:auto;padding:1em}.modal[hidden]{display:none}
.panel{background:white;color:black;padding:1em;margin:2em auto;max-width:600px;border-radius:12px}textarea,input{box-sizing:border-box;width:100%;font-size:1rem}textarea{min-height:6em}
.sticky{position:sticky;top:0;background:#fff;padding:.5em;z-index:2}</style>
<h1>確認が必要そうな箇所</h1><p>字幕を焼き込む前の確認用動画です。時刻は動画の先頭からの目安です。
まずGeminiが音声から挙げた確認候補と、機械的に検出した話者の切り替わりなどをまとめています。
候補は推定で、確認済みを意味せず、全ての誤りを検出できるわけではありません。
ほかの語の変更と全字幕は、必要なときだけ下の項目を開いて確認できます。
字幕の「編集」を押すと文字と時刻を直せます。動画上の字幕は確認用の簡易表示です。</p>
<p>動画が表示されない場合は、各動画の「動画ファイルを選ぶ」から、同じ番号の確認用動画を選んでください。ファイルはこの画面の中で再生します。</p>
<div class="sticky"><button type="button" onclick="saveEdits()">修正ファイルを保存</button>
<label>保存済みの修正を読み込む <input type="file" accept="application/json,.json" onchange="loadEdits(this)"></label>
<span id="edit-count">変更 0 件</span></div>
<p>修正は自動送信されません。保存したJSONファイルをこの会話に送ってください。画面を閉じる前に保存してください。</p>"""
    ]
    order = []
    for index, (clip, transcript, raw, provenance) in enumerate(
        zip(selection["clips"], transcripts["clips"], raws, provenances), 1
    ):
        rows = review_rows(clip, transcript, raw, provenance)
        changes = sum("清書で語が変わった可能性" in row["reasons"] for row in rows)
        unresolved = sum(not row["mapping_exact"] for row in rows)
        order.append((unresolved > 0, changes, index, unresolved))
    parts.append(
        "<h2>確認の順番の参考</h2><p>語の変更が少ない動画から並べています。見る順番は自由です。</p><ol>"
    )
    for _, changes, index, unresolved in sorted(order):
        parts.append(
            f'<li><a href="#clip{index}">動画 {index}</a>：語の変更 {changes}行、対応不明 {unresolved}行</li>'
        )
    parts.append("</ol>")
    for index, (clip, transcript, raw, provenance) in enumerate(
        zip(selection["clips"], transcripts["clips"], raws, provenances), 1
    ):
        rows = review_rows(clip, transcript, raw, provenance)
        parts.append(
            f'<section id="clip{index}"><h2>{index}. {esc(clip["title"])}</h2>'
        )
        parts.append(
            f"<p>元配信 {clock(clip['start'])}〜{clock(clip['end'])}　見出しと内容を確認</p>"
        )
        parts.append(
            f'<div class="viewer"><video id="v{index}" controls preload="metadata" src="clip-{index:03}.mp4"></video><div class="overlay" id="overlay{index}" onclick="editAt({index})"></div></div>'
        )
        parts.append(f'<label>動画 {index} の動画ファイルを選ぶ <input type="file" accept="video/*" onchange="selectVideo({index},this)"></label>')
        parts.append(f'<p><button type="button" onclick="addCaption({index})">現在の位置に字幕を追加</button></p><div id="added{index}"></div>')
        hints = provenance.get("review_hints", [])
        status = provenance.get("review_hints_status", "not_returned")
        parts.append('<h3>Geminiが挙げた確認候補</h3>')
        if status not in {"available", "partial"}:
            parts.append('<p>Geminiの確認候補は取得されていません。候補なしとは判断できません。</p>')
        elif not hints and status == "available":
            parts.append('<p>Geminiは候補を挙げていません。誤りがないという意味ではありません。</p>')
        else:
            if status == "partial":
                parts.append('<p>形式が不明な候補があり、一部を表示できません。</p>')
            parts.append('<p>時刻は文字起こしから引き継いだ推定値です。話者は匿名ラベルです。</p>')
            parts.append('<table><thead><tr><th>時刻・字幕番号</th><th>確認する理由・話者ラベル</th><th>文字起こし → 清書後</th></tr></thead><tbody>')
            for hint in hints:
                ids = set(hint["source_ids"])
                matched = [row for row in rows if ids.intersection(row["source_ids"])]
                if not matched:
                    for sid in hint["source_ids"]:
                        source = raw["segments"][sid - 1]
                        parts.append(f'<tr><td><button type="button" onclick="seek({index},{max(0, float(source["start"]) - 2):.3f})">{clock(source["start"])}〜{clock(source["end"])}</button></td>'
                                     f'<td>{esc(hint["reason"])}</td><td>{esc(source["text"])} → 字幕への対応不明</td></tr>')
                for row in matched:
                    parts.append(f'<tr><td><button type="button" onclick="seek({index},{max(0, float(row["start"]) - 2):.3f})">{clock(row["start"])}〜{clock(row["end"])}</button> #{row["number"]}</td>'
                                 f'<td>{esc(hint["reason"])}<br><small>{esc(", ".join(row["speakers"]))}</small> <button type="button" onclick="editCaption({index},{row["number"]})">編集</button></td>'
                                 f'<td>{esc(row["original"] or "元の文との対応不明")} → {esc(row["text"])}</td></tr>')
            parts.append('</tbody></table>')
        parts.append(
            f'<p>境界の確認：<button type="button" onclick="seek({index},0)">冒頭30秒</button> '
            f'<button type="button" onclick="seek({index},{max(0, clip["end"] - clip["start"] - 30):.3f})">末尾30秒</button></p>'
        )
        groups = focused_groups(rows)
        parts.append(
            f"<h3>優先して確認する箇所（{len(groups)}区間）</h3><p>近い話者の切り替わりなどをまとめています。</p><ul>"
        )
        for group in groups:
            start = group[0]["start"]
            end = max(row["end"] for row in group)
            reasons = sorted({reason for row in group for reason in row["reasons"]
                              if any(key in reason for key in ("話者交代", "話者不明", "元IDの対応不明"))})
            if any(len(row["speakers"]) > 1 for row in group):
                reasons.append("複数の話者ラベルが重なる可能性")
            parts.append(
                f'<li><button type="button" onclick="seek({index},{max(0, float(start) - 2):.3f})">'
                f"{clock(start)}〜{clock(end)}</button> {esc('、'.join(reasons))} "
                f"（字幕 {group[0]['number']}〜{group[-1]['number']}）"
            )
            parts.append('<details><summary>この区間の字幕候補を開く</summary><ul>')
            for row in group:
                parts.append(f'<li><button type="button" onclick="seek({index},{max(0, float(row["start"]) - 2):.3f})">'
                             f'{clock(row["start"])}</button> {esc(row["text"])} '
                             f'<small>（{esc(", ".join(row["speakers"]))}）</small> <button type="button" onclick="editCaption({index},{row["number"]})">編集</button></li>')
            parts.append('</ul></details></li>')
        parts.append("</ul>")
        if not groups:
            parts.append("<p>話者交代・重複・判別不明の候補は見つかっていません。</p>")
        changed = sum("清書で語が変わった可能性" in row["reasons"] for row in rows)
        parts.append(f'<details><summary>参考：語の変更と全文を見る（変更の可能性 {changed} 行）</summary>')
        if changed:
            parts.append('<h3>語の変更一覧</h3><table><thead><tr><th>時刻・字幕番号</th><th>文字起こし → 清書後</th></tr></thead><tbody>')
            for row in rows:
                if "清書で語が変わった可能性" in row["reasons"]:
                    parts.append(f'<tr><td><button type="button" onclick="seek({index},{max(0, float(row["start"]) - 2):.3f})">{clock(row["start"])}〜{clock(row["end"])}</button> #{row["number"]}</td><td>{esc(row["original"])} → {esc(row["text"])}</td></tr>')
            parts.append('</tbody></table>')
        unchanged = [
            row
            for row in rows
            if row["mapping_exact"]
            and normalized(row["original"]) == normalized(row["text"])
        ]
        seed = hashlib.sha256(
            json.dumps([clip, transcript], ensure_ascii=False, sort_keys=True).encode()
        ).digest()
        samples = sorted(
            random.Random(seed).sample(unchanged, min(10, len(unchanged))),
            key=lambda row: row["start"],
        )
        parts.append(
            f"<h3>変更なしの抜き取り（{len(samples)}行）</h3><p>必要に応じて見るための参考です。誤りがあれば、追加で確認する範囲を選べます。</p><ul>"
        )
        for row in samples:
            parts.append(
                f'<li><button type="button" onclick="seek({index},{max(0, float(row["start"]) - 2):.3f})">'
                f"{clock(row['start'])}</button> {esc(row['text'])}</li>"
            )
        parts.append("</ul><h3>全字幕</h3>")
        parts.append(
            "<table><thead><tr><th>番号・時刻</th><th>字幕</th><th>確認候補</th></tr></thead><tbody>"
        )
        for row in rows:
            reasons = "、".join(row["reasons"])
            original = (
                f"<br><small>文字起こし: {esc(row['original'])}</small>"
                if row["original"]
                and normalized(row["original"]) != normalized(row["text"])
                else ""
            )
            parts.append(
                f'<tr class="{"flag" if reasons else ""}"><td>{row["number"]} '
                f'<button type="button" onclick="seek({index},{float(row["start"]):.3f})">{clock(row["start"])}</button> <button type="button" onclick="editCaption({index},{row["number"]})">編集</button></td>'
                f"<td><span id=\"caption-{index}-{row['number']}\">{esc(row['text'])}</span>{original}</td><td>{esc(reasons)} "
                f"<small>{esc(', '.join(row['speakers']))}</small></td></tr>"
            )
        parts.append("</tbody></table>")
        if not rows:
            parts.append(
                "<p>時刻付き字幕がありません。動画の音声を確認してください。</p>"
            )
        used_ids = {source_id for row in rows for source_id in row["source_ids"]}
        missing = [
            source
            for i, source in enumerate(raw.get("segments", []), 1)
            if i not in used_ids
        ]
        if missing:
            parts.append("<h3>字幕への対応が見つからなかった文字起こし</h3><ul>")
            for source in missing:
                parts.append(
                    f'<li><button type="button" onclick="seek({index},{float(source["start"]):.3f})">'
                    f"{clock(source['start'])}</button> {esc(source['text'])}</li>"
                )
            parts.append("</ul>")
        if raw.get("untimed"):
            parts.append(
                "<p>時刻を特定できなかった文字起こし（音声で確認）: "
                + esc(" / ".join(str(row.get("text", "")) for row in raw["untimed"]))
                + "</p>"
            )
        parts.append("</details>")
        parts.append("</section>")
    editor_data = {"fingerprint": fingerprint({"selection": selection, "transcripts": transcripts}),
                   "clips": [{"duration": clip["end"] - clip["start"], "segments": data["segments"]}
                             for clip, data in zip(selection["clips"], transcripts["clips"])]}
    encoded = json.dumps(editor_data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    parts.append('<div class="modal" id="caption-editor" hidden><div class="panel"><h2 id="editor-title">字幕を編集</h2><label>字幕の文章<textarea id="editor-text"></textarea></label><label>開始時刻（秒）<input id="editor-start" type="number" min="0" step="0.1" inputmode="decimal"></label><label>終了時刻（秒）<input id="editor-end" type="number" min="0" step="0.1" inputmode="decimal"></label><p><button type="button" onclick="applyCaption()">この字幕を反映</button> <button type="button" onclick="closeEditor()">閉じる</button></p></div></div>')
    parts.append(f'<script type="application/json" id="editor-data">{encoded}</script>')
    parts.append(r'''<script>
const editorData = JSON.parse(document.getElementById("editor-data").textContent);
let edits = new Map(), additions = [], editing = null;
const selectedVideos = new Map();
function selectVideo(i,input){const file=input.files&&input.files[0];if(!file)return;
 const v=document.getElementById("v"+i),previous=selectedVideos.get(i),url=URL.createObjectURL(file);
 v.src=url;selectedVideos.set(i,url);if(previous)URL.revokeObjectURL(previous);v.load()}
function seek(i,t){const v=document.getElementById("v"+i);v.currentTime=t;v.play();v.scrollIntoView({behavior:"smooth",block:"center"})}
function key(c,n){return c+":"+n}
function caption(c,n){return edits.get(key(c,n)) || editorData.clips[c-1].segments[n-1]}
function editCaption(c,n){editing={clip:c,number:n};showEditor(caption(c,n),"字幕 "+n+" を編集")}
function addCaption(c){const v=document.getElementById("v"+c), duration=editorData.clips[c-1].duration;
 const start=Math.max(0,Math.min(v.currentTime||0,Math.max(0,duration-0.1)));
 editing={clip:c,number:0};showEditor({text:"",start:start,end:Math.min(duration,start+2)},"字幕を追加")}
function editAdded(i){editing={clip:additions[i].clip,number:0,added:i};showEditor(additions[i],"追加した字幕を編集")}
function showEditor(data,title){document.getElementById("editor-title").textContent=title;
 document.getElementById("editor-text").value=data.text;document.getElementById("editor-start").value=String(data.start);
 document.getElementById("editor-end").value=String(data.end);document.getElementById("caption-editor").hidden=false}
function closeEditor(){document.getElementById("caption-editor").hidden=true;editing=null}
function applyCaption(){if(!editing)return;const text=document.getElementById("editor-text").value;
 const a=document.getElementById("editor-start").value,b=document.getElementById("editor-end").value;
 const start=Number(a),end=Number(b),duration=editorData.clips[editing.clip-1].duration;
 if(!a.trim()||!b.trim()||!Number.isFinite(start)||!Number.isFinite(end)||start<0||start>=end||end>duration){alert("開始・終了時刻を動画の範囲内で正しく入力してください。");return}
 if(!editing.number&&!text.trim()){alert("追加する字幕の文章を入力してください。");return}
 if(editing.number){const base=editorData.clips[editing.clip-1].segments[editing.number-1];
 const value={start,end,text};if(base.start===start&&base.end===end&&base.text===text)edits.delete(key(editing.clip,editing.number));
 else edits.set(key(editing.clip,editing.number),value)}else if(editing.added!==undefined){additions[editing.added]={clip:editing.clip,start,end,text}}
 else additions.push({clip:editing.clip,start,end,text});closeEditor();refresh()}
function removeAdded(i){additions.splice(i,1);refresh()}
function refresh(){document.getElementById("edit-count").textContent="変更 "+(edits.size+additions.length)+" 件";
 editorData.clips.forEach((clip,idx)=>{const c=idx+1;clip.segments.forEach((_,j)=>{const el=document.getElementById("caption-"+c+"-"+(j+1));if(el)el.textContent=caption(c,j+1).text});
 const box=document.getElementById("added"+c);box.replaceChildren();additions.forEach((item,i)=>{if(item.clip!==c)return;
 const p=document.createElement("p"), label=document.createElement("span"), e=document.createElement("button"), d=document.createElement("button");
 label.textContent="追加 "+item.start.toFixed(1)+"〜"+item.end.toFixed(1)+"秒: "+item.text+" ";e.textContent="編集";e.onclick=()=>editAdded(i);d.textContent="取り消し";d.onclick=()=>removeAdded(i);p.append(label,e,d);box.append(p)});
 updateOverlay(c)})}
function updateOverlay(c){const v=document.getElementById("v"+c),t=v.currentTime;
 const lines=editorData.clips[c-1].segments.map((_,j)=>caption(c,j+1)).concat(additions.filter(x=>x.clip===c));
 const shown=lines.filter(x=>x.start<=t&&t<x.end&&x.text);const overlay=document.getElementById("overlay"+c);
 overlay.textContent=shown.map(x=>x.text).join("\n");overlay.style.pointerEvents=shown.length?"auto":"none"}
function editAt(c){const t=document.getElementById("v"+c).currentTime,active=[];
 editorData.clips[c-1].segments.forEach((_,i)=>{const row=caption(c,i+1);if(row.start<=t&&t<row.end&&row.text)active.push({number:i+1})});
 additions.forEach((row,i)=>{if(row.clip===c&&row.start<=t&&t<row.end&&row.text)active.push({added:i})});
 if(active.length!==1){alert("複数の字幕が重なっています。下の字幕一覧から編集してください。");return}
 document.getElementById("v"+c).pause();if(active[0].number)editCaption(c,active[0].number);else editAdded(active[0].added)}
function buildPatch(){const clips=[];editorData.clips.forEach((_,i)=>{const c=i+1,segments=[];
 edits.forEach((v,k)=>{const [clip,n]=k.split(":").map(Number);if(clip===c)segments.push({segment:n,...v})});
 const added=additions.filter(x=>x.clip===c).map(({start,end,text})=>({start,end,text}));
 if(segments.length||added.length)clips.push({clip:c,segments,...(added.length?{add_segments:added}:{})})});
 return {reason:"利用者による字幕修正",input_fingerprint:editorData.fingerprint,clips}}
function saveEdits(){const patch=buildPatch();if(!patch.clips.length){alert("まだ修正がありません。");return}
 const url=URL.createObjectURL(new Blob([JSON.stringify(patch,null,2)],{type:"application/json"}));const a=document.createElement("a");
 a.href=url;a.download="clipforge-subtitle-edits.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function loadEdits(input){try{const patch=JSON.parse(await input.files[0].text());if(patch.input_fingerprint!==editorData.fingerprint||!Array.isArray(patch.clips))throw Error();
 const next=new Map(),extra=[];for(const row of patch.clips){const c=row.clip,clip=editorData.clips[c-1];if(!Number.isInteger(c)||!clip)throw Error();
 for(const change of row.segments||[]){if(!Number.isInteger(change.segment)||!clip.segments[change.segment-1]||typeof change.text!=="string"||!Number.isFinite(change.start)||!Number.isFinite(change.end)||change.start<0||change.start>=change.end||change.end>clip.duration)throw Error();next.set(key(c,change.segment),{start:change.start,end:change.end,text:change.text})}
 for(const item of row.add_segments||[]){if(typeof item.text!=="string"||!item.text.trim()||!Number.isFinite(item.start)||!Number.isFinite(item.end)||item.start<0||item.start>=item.end||item.end>clip.duration)throw Error();extra.push({clip:c,start:item.start,end:item.end,text:item.text})}}
 edits=next;additions=extra;refresh()}catch(e){alert("この動画の修正ファイルを読み込めませんでした。")}input.value=""}
editorData.clips.forEach((_,i)=>{const c=i+1;document.getElementById("v"+c).addEventListener("timeupdate",()=>updateOverlay(c))});refresh();
</script></html>''')
    return "\n".join(parts)


def create(editorial, output, config):
    editorial, output = Path(editorial), Path(output)
    if output.exists():
        raise PipelineError("Review package already exists")
    selection = json.loads((editorial / "final-selection.json").read_text())
    transcripts = json.loads((editorial / "selected-transcripts.json").read_text())
    clips = selection.get("clips", [])
    if (
        selection.get("selection_basis") != "audio_understanding"
        or not clips
        or len(clips) != len(transcripts.get("clips", []))
    ):
        raise PipelineError("Saved editorial selection and transcripts do not match")
    raws = [
        json.loads((editorial / f"transcribe-selected-{i:03}.json").read_text())
        for i in range(1, len(clips) + 1)
    ]
    provenances = [
        json.loads((editorial / f"review-source-{i:03}.json").read_text())
        for i in range(1, len(clips) + 1)
    ]
    settings = Settings.model_validate(json.loads(Path(config).read_text()))
    with tempfile.TemporaryDirectory(prefix="clipforge-review-") as tmp:
        root = Path(tmp)
        vod = open_source(
            canonical(selection["source_url"]),
            root / "cache",
            max_bytes=settings.max_transport_bytes,
            max_seconds=settings.max_source_seconds,
        )
        try:
            for i, clip in enumerate(clips, 1):
                retrieved(
                    vod,
                    root / f"clip-{i:03}.mp4",
                    clip["start"],
                    clip["end"],
                    quality="preview",
                )
        finally:
            close = getattr(vod, "close", None)
            if close:
                close()
        (root / "review.html").write_text(
            make_page(selection, transcripts, raws, provenances), encoding="utf-8"
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".review-package-", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as bundle:
                bundle.write(root / "review.html", "review.html")
                for name in ("final-selection.json", "selected-transcripts.json"):
                    bundle.write(editorial / name, name)
                for i in range(1, len(clips) + 1):
                    bundle.write(root / f"clip-{i:03}.mp4", f"clip-{i:03}.mp4")
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)


def collect_package(run_id, output, state, save):
    """Read the private asset through REST; never dispatch or replace a saved file."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise PipelineError("Invalid review run")
    private_repo()
    tag = f"clipforge-kick-editorial-{run_id}"
    release = json.loads(gh("api", f"repos/{REPO}/releases/tags/{tag}"))
    assets = [
        asset
        for asset in release.get("assets", [])
        if asset.get("name") == "review-package.zip"
    ]
    if (
        len(assets) != 1
        or type(assets[0].get("size")) is not int
        or not 0 < assets[0]["size"] <= MAX_BYTES
    ):
        raise PipelineError("Private review package is missing or ambiguous")
    asset = assets[0]
    if type(asset.get("id")) is not int or asset["id"] <= 0:
        raise PipelineError("Invalid review package asset")
    destination = Path(output) / "review-package.zip"
    saved = state.get("review_package", {})
    if destination.exists():
        if (
            not destination.is_file()
            or destination.is_symlink()
            or saved.get("asset_id") != asset["id"]
            or saved.get("size") != destination.stat().st_size
            or saved.get("size") != asset["size"]
        ):
            raise PipelineError(
                "Existing review package has no matching saved identity"
            )
        digest = hashlib.sha256()
        with destination.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if saved.get("sha256") != digest.hexdigest():
            raise PipelineError("Existing review package has changed")
        inspect_archive(destination, state.get("source_url"))
        return destination
    fd, temporary_name = tempfile.mkstemp(prefix=".review-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as out:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{REPO}/releases/assets/{asset['id']}",
                    "-H",
                    "Accept: application/octet-stream",
                ],
                stdout=out,
                stderr=subprocess.PIPE,
                timeout=1800,
            )
        if result.returncode or temporary.stat().st_size != asset["size"]:
            raise PipelineError("Private review download failed or has the wrong size")
        digest = hashlib.sha256()
        with temporary.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        asset_digest = asset.get("digest")
        if asset_digest and asset_digest != "sha256:" + digest.hexdigest():
            raise PipelineError("Private review package digest mismatch")
        inspect_archive(temporary, state.get("source_url"))
        state["review_package"] = {
            "asset_id": asset["id"],
            "size": asset["size"],
            "sha256": digest.hexdigest(),
        }
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
