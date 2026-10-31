"""Titles, descriptions, tags and X text for a finished clip.

The template is always built first from what ClipForge recorded (clip title,
selection reason, source) and is a complete result on its own. When a Vertex
access token is available, Gemini may tighten the title and lead sentence; any
failure or odd answer falls back to the template, so publishing never waits on it.
"""

import json
import os

from . import http
from .x import fit

PROJECT = "project-fdadb4dd-cb77-4270-9cd"
RULES = """あなたは日本語の切り抜き動画の編集者です。与えられた情報だけを使い、
YouTubeのタイトル（全角30文字前後、最大60文字）と説明文の書き出し（2文以内）を作ります。
- 内容にない出来事・数字・感情を足さない。誇張や釣り、断定できないことの断定をしない。
- 配信者名と話題の具体的な対象を入れ、見る理由が分かるようにする。
- 記号の乱用・全角英数の混在・絵文字の多用をしない。ハッシュタグは本文に入れない。
JSONで {"title": "...", "lead": "...", "hashtags": ["...", "..."]} だけを返します。"""


def template(clip, job, channel, config):
    title = clip.get("title") or job.get("title") or "切り抜き"
    lines = []
    if clip.get("reason"):
        lines.append(str(clip["reason"]).strip())
    lines += ["", f"元配信: {job.get('title', '').strip()}", job.get("url", "")]
    if channel.credit:
        lines.append(channel.credit)
    if config.description_footer:
        lines += ["", config.description_footer]
    hashtags = ["#切り抜き", "#" + "".join(channel.name.split())]
    lines += ["", " ".join(hashtags)]
    tags = [*channel.tags, channel.name, "切り抜き"]
    return {
        "title": title,
        "description": "\n".join(lines).strip(),
        "tags": tags,
        "hashtags": hashtags,
    }


class _Blank(dict):
    def __missing__(self, key):
        return ""


def x_text(meta, url, config):
    """X post text from the configured template; unknown placeholders become empty."""
    fields = _Blank(title=meta["title"], hashtags=" ".join(meta.get("hashtags", [])))
    try:
        text = config.x_template.replace("{url}", "").format_map(fields).strip()
    except (ValueError, IndexError):
        text = meta["title"]
    return fit(text, url)


def _gemini(prompt, model, token, request):
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"role": "system", "parts": [{"text": RULES}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "MINIMAL"},
            "maxOutputTokens": 1024,
        },
    }
    url = (
        f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/"
        f"publishers/google/models/{model}:generateContent"
    )
    reply = request(
        "POST",
        url,
        headers={"Authorization": f"Bearer {token}"},
        json_body=body,
        retries=3,
        timeout=90,
    ).json()
    text = "".join(
        part.get("text", "")
        for part in reply["candidates"][0]["content"]["parts"]
        if not part.get("thought")
    )
    return json.loads(text)


def build(clip, job, channel, config, env=os.environ, request=http.request, log=print):
    meta = template(clip, job, channel, config)
    token = env.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if not token:
        return meta
    prompt = json.dumps(
        {
            "配信者": channel.name,
            "元配信タイトル": job.get("title", ""),
            "切り抜きの見出し": clip.get("title", ""),
            "選んだ理由": clip.get("reason", ""),
        },
        ensure_ascii=False,
    )
    try:
        answer = _gemini(prompt, config.metadata_model, token, request)
        title = " ".join(str(answer.get("title", "")).split())
        lead = str(answer.get("lead", "")).strip()
        if 4 <= len(title) <= 60:
            meta["title"] = title
        if lead and len(lead) <= 300:
            meta["description"] = lead + "\n\n" + meta["description"]
        extra = [
            "#" + str(tag).lstrip("#").replace(" ", "")
            for tag in answer.get("hashtags", [])[:3]
            if str(tag).strip("# ")
        ]
        meta["hashtags"] = list(dict.fromkeys(meta["hashtags"] + extra))
    except (http.HttpError, KeyError, IndexError, TypeError, ValueError) as error:
        log(f"[metadata] Gemini を使わずテンプレートで続行: {error}")
    return meta
