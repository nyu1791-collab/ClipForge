"""YouTube Data API v3 + YouTube Analytics API with the owner's OAuth refresh token.

Scopes requested once by ``autopilot auth-youtube``:
youtube.upload (upload), youtube (change privacy, thumbnails),
yt-analytics.readonly (views and retention).

Uploads are resumable: a dropped connection resumes from the byte the server
confirmed, so one clip never becomes two videos. Note: until the Google Cloud
project passes YouTube's API audit, every API upload is forced to private.
"""

import os
import time
from pathlib import Path

from . import http

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3/"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
CHUNK = 8 * 1024 * 1024  # a multiple of 256 KiB, as resumable uploads require


class YouTubeError(RuntimeError):
    pass


class Credentials:
    """Access token from a refresh token; refreshed a minute before expiry."""

    def __init__(
        self,
        client_id,
        client_secret,
        refresh_token,
        request=http.request,
        clock=time.time,
    ):
        if not (client_id and client_secret and refresh_token):
            raise YouTubeError(
                "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN が必要です"
            )
        self._form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        self.request = request
        self.clock = clock
        self._token, self._expires = None, 0.0

    @classmethod
    def from_env(cls, env=os.environ, **kwargs):
        return cls(
            env.get("YOUTUBE_CLIENT_ID"),
            env.get("YOUTUBE_CLIENT_SECRET"),
            env.get("YOUTUBE_REFRESH_TOKEN"),
            **kwargs,
        )

    def token(self):
        if not self._token or self.clock() > self._expires - 60:
            reply = self.request("POST", TOKEN_URL, form=self._form, retries=3).json()
            self._token = reply["access_token"]
            self._expires = self.clock() + float(reply.get("expires_in", 3600))
        return self._token

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}"}


def clean_title(title):
    """YouTube titles: at most 100 characters, no angle brackets."""
    text = " ".join(str(title).replace("<", "＜").replace(">", "＞").split())
    return text[:100] or "無題"


def clean_tags(tags):
    """At most 500 characters in total across tags; drop the rest."""
    kept, total = [], 0
    for tag in tags:
        tag = str(tag).replace("<", "").replace(">", "").replace(",", " ").strip()
        if not tag or tag in kept:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if kept else 0)
        if total + cost > 500:
            break
        kept.append(tag)
        total += cost
    return kept


def upload(
    credentials,
    path,
    *,
    title,
    description,
    tags=(),
    category="20",
    privacy="private",
    publish_at=None,
    request=http.request,
    chunk=CHUNK,
    sleep=time.sleep,
):
    """Resumable upload. Returns the new video ID."""
    path = Path(path)
    size = path.stat().st_size
    status = {
        "privacyStatus": "private" if publish_at else privacy,
        "selfDeclaredMadeForKids": False,
        "embeddable": True,
    }
    if publish_at:
        status["publishAt"] = publish_at
    body = {
        "snippet": {
            "title": clean_title(title),
            "description": str(description)[:5000],
            "tags": clean_tags(tags),
            "categoryId": str(category),
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": status,
    }
    start = request(
        "POST",
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            **credentials.headers(),
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
        json_body=body,
    )
    location = start.headers.get("location")
    if not location:
        raise YouTubeError("アップロード先URLを受け取れませんでした")
    offset, failures = 0, 0
    with path.open("rb") as handle:
        while True:
            handle.seek(offset)
            data = handle.read(chunk)
            end = offset + len(data) - 1
            try:
                reply = request(
                    "PUT",
                    location,
                    headers={
                        **credentials.headers(),
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{end}/{size}"
                        if data
                        else f"bytes */{size}",
                    },
                    data=data,
                    ok=(200, 201, 308),
                    timeout=300,
                )
            except http.HttpError as error:
                if error.status not in (0, 500, 502, 503, 504) or failures >= 5:
                    raise
                failures += 1
                sleep(min(2**failures, 30))
                offset = _confirmed(credentials, location, size, request)
                continue
            if reply.status in (200, 201):
                return reply.json()["id"]
            failures = 0
            confirmed = reply.headers.get("range", "")
            offset = int(confirmed.rsplit("-", 1)[1]) + 1 if confirmed else 0


def _confirmed(credentials, location, size, request):
    """Ask how many bytes the server has, after a dropped chunk."""
    reply = request(
        "PUT",
        location,
        headers={**credentials.headers(), "Content-Range": f"bytes */{size}"},
        data=b"",
        ok=(200, 201, 308),
    )
    confirmed = reply.headers.get("range", "")
    return int(confirmed.rsplit("-", 1)[1]) + 1 if confirmed else 0


def set_privacy(credentials, video_id, privacy, publish_at=None, request=http.request):
    status = {
        "privacyStatus": "private" if publish_at else privacy,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at:
        status["publishAt"] = publish_at
    request(
        "PUT",
        API + "videos",
        params={"part": "status"},
        headers=credentials.headers(),
        json_body={"id": video_id, "status": status},
    )


def set_thumbnail(credentials, video_id, image, request=http.request):
    """Custom thumbnails need a verified channel; callers treat failure as optional."""
    request(
        "POST",
        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
        params={"videoId": video_id},
        headers={**credentials.headers(), "Content-Type": "image/jpeg"},
        data=Path(image).read_bytes(),
    )


def videos(credentials, ids, request=http.request):
    """Current status of uploaded videos (processing, privacy)."""
    found = {}
    ids = list(ids)
    for i in range(0, len(ids), 50):
        reply = request(
            "GET",
            API + "videos",
            params={
                "part": "status,processingDetails,snippet",
                "id": ",".join(ids[i : i + 50]),
            },
            headers=credentials.headers(),
        ).json()
        for item in reply.get("items", []):
            found[item["id"]] = item
    return found


def analytics(credentials, ids, start_date, end_date, request=http.request):
    """Per-video views and retention. Returns {video_id: {metric: value}}."""
    metrics = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,subscribersGained"
    result = {}
    ids = list(ids)
    for i in range(0, len(ids), 200):
        reply = request(
            "GET",
            ANALYTICS,
            params={
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": metrics,
                "dimensions": "video",
                "filters": "video==" + ",".join(ids[i : i + 200]),
                "maxResults": 200,
            },
            headers=credentials.headers(),
        ).json()
        names = [h["name"] for h in reply.get("columnHeaders", [])]
        for row in reply.get("rows", []) or []:
            values = dict(zip(names, row))
            result[values.pop("video")] = values
    return result


def authorize_url(client_id, redirect_uri, state):
    from urllib.parse import urlencode

    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )


def exchange_code(client_id, client_secret, code, redirect_uri, request=http.request):
    reply = request(
        "POST",
        TOKEN_URL,
        form={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    ).json()
    if "refresh_token" not in reply:
        raise YouTubeError(
            "refresh_token が返りませんでした。同意画面をやり直してください"
        )
    return reply["refresh_token"]
