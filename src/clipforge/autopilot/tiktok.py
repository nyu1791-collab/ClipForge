"""TikTok Content Posting API: send a video to the creator's inbox (drafts).

The owner opens TikTok, adds sound/caption and taps post. Direct posting needs
TikTok's app audit; until then drafts are the sanctioned route.
Env: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN (scope video.upload).
"""

import math
import os
from pathlib import Path

from . import http

API = "https://open.tiktokapis.com/v2/"
MIN_CHUNK = 5 * 1024 * 1024
MAX_SINGLE = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


def access_token(env=os.environ, request=http.request):
    key, secret, refresh = (
        env.get("TIKTOK_CLIENT_KEY"),
        env.get("TIKTOK_CLIENT_SECRET"),
        env.get("TIKTOK_REFRESH_TOKEN"),
    )
    if not (key and secret and refresh):
        raise TikTokError(
            "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN が必要です"
        )
    reply = request(
        "POST",
        API + "oauth/token/",
        form={
            "client_key": key,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        retries=3,
    ).json()
    if "access_token" not in reply:
        raise TikTokError(f"アクセストークンを取得できません: {http.redact(reply)}")
    return reply["access_token"]


def plan_chunks(size):
    """TikTok rules: one chunk up to 64 MB; otherwise 5–64 MB chunks, the last
    chunk absorbing the remainder."""
    if size <= MAX_SINGLE:
        return size, 1
    return CHUNK, math.floor(size / CHUNK)


def send_to_inbox(path, token, request=http.request):
    path = Path(path)
    size = path.stat().st_size
    chunk, count = plan_chunks(size)
    init = request(
        "POST",
        API + "post/publish/inbox/video/init/",
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk,
                "total_chunk_count": count,
            }
        },
    ).json()
    data = init.get("data") or {}
    if not data.get("upload_url"):
        raise TikTokError(
            f"アップロード先を受け取れません: {http.redact(init.get('error'))}"
        )
    with path.open("rb") as handle:
        for index in range(count):
            start = index * chunk
            length = size - start if index == count - 1 else chunk
            handle.seek(start)
            request(
                "PUT",
                data["upload_url"],
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {start}-{start + length - 1}/{size}",
                },
                data=handle.read(length),
                ok=(200, 201, 206),
                timeout=300,
            )
    return data["publish_id"]
