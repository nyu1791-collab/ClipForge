"""Find finished VODs on watched channels.

- YouTube: the public channel feed (no key), then the Data API (API key or the
  owner's OAuth token) to drop live/upcoming streams and short uploads. Without
  any API access, only entries older than ``settle_hours`` are taken, so a stream
  that is still live is not picked up.
- Twitch: Helix with an app token; the archive of a stream that is still live
  is skipped until it ends.
- Kick: its public channel endpoint. It is unofficial and may be blocked; a
  failure is reported for that channel only.
"""

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

from . import http

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass(frozen=True)
class Vod:
    platform: str
    channel: str
    video_id: str
    url: str
    title: str
    published_at: float
    duration_seconds: float | None = None

    @property
    def key(self):
        return f"{self.platform}:{self.video_id}"


def _timestamp(value):
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso_duration(value):
    """PT1H2M3S -> 3723.0 (YouTube contentDetails.duration)."""
    match = re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value or ""
    )
    if not match:
        return None
    d, h, m, s = (float(x or 0) for x in match.groups())
    return d * 86400 + h * 3600 + m * 60 + s


def twitch_duration(value):
    """3h2m1s -> 10921.0 (Helix video duration)."""
    parts = dict(
        (unit, float(n)) for n, unit in re.findall(r"(\d+)([hms])", value or "")
    )
    if not parts:
        return None
    return parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)


def parse_feed(xml_text, channel):
    root = ET.fromstring(xml_text)
    vods = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=NS)
        if not video_id:
            continue
        vods.append(
            Vod(
                platform="youtube",
                channel=channel,
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=entry.findtext("atom:title", default="", namespaces=NS),
                published_at=_timestamp(
                    entry.findtext("atom:published", namespaces=NS)
                ),
            )
        )
    return vods


def youtube(
    channel, *, request=http.request, api_auth=None, now=None, settle_hours=8.0
):
    """api_auth: {"key": API key} or {"token": OAuth access token} or None."""
    now = now or time.time()
    feed = request("GET", FEED.format(channel.id)).text()
    vods = parse_feed(feed, channel.key)
    if not vods:
        return []
    if not api_auth:
        return [v for v in vods if now - v.published_at >= settle_hours * 3600]
    params = {
        "part": "snippet,contentDetails,liveStreamingDetails",
        "id": ",".join(v.video_id for v in vods),
    }
    headers = {}
    if api_auth.get("key"):
        params["key"] = api_auth["key"]
    else:
        headers["Authorization"] = f"Bearer {api_auth['token']}"
    details = request(
        "GET",
        "https://www.googleapis.com/youtube/v3/videos",
        params=params,
        headers=headers,
    ).json()
    info = {item["id"]: item for item in details.get("items", [])}
    finished = []
    for vod in vods:
        item = info.get(vod.video_id)
        if not item:
            continue  # private, deleted or not yet processed
        if item.get("snippet", {}).get("liveBroadcastContent", "none") != "none":
            continue
        live = item.get("liveStreamingDetails")
        if live and not live.get("actualEndTime"):
            continue
        duration = iso_duration(item.get("contentDetails", {}).get("duration"))
        finished.append(
            Vod(
                vod.platform,
                vod.channel,
                vod.video_id,
                vod.url,
                vod.title,
                _timestamp(live.get("actualEndTime")) if live else vod.published_at,
                duration,
            )
        )
    return finished


def twitch_token(client_id, client_secret, request=http.request):
    reply = request(
        "POST",
        "https://id.twitch.tv/oauth2/token",
        form={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
    ).json()
    return reply["access_token"]


def twitch(channel, *, client_id, token, request=http.request):
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    helix = "https://api.twitch.tv/helix/"
    users = request(
        "GET", helix + "users", params={"login": channel.id}, headers=headers
    ).json()
    if not users.get("data"):
        raise LookupError(f"Twitch ユーザーが見つかりません: {channel.id}")
    user_id = users["data"][0]["id"]
    streams = request(
        "GET", helix + "streams", params={"user_id": user_id}, headers=headers
    ).json()
    live_stream = next(
        (s["id"] for s in streams.get("data", []) if s.get("type") == "live"), None
    )
    videos = request(
        "GET",
        helix + "videos",
        params={"user_id": user_id, "type": "archive", "first": 5},
        headers=headers,
    ).json()
    vods = []
    for item in videos.get("data", []):
        if live_stream and item.get("stream_id") == live_stream:
            continue  # still being recorded
        duration = twitch_duration(item.get("duration"))
        created = _timestamp(item.get("created_at"))
        vods.append(
            Vod(
                "twitch",
                channel.key,
                str(item["id"]),
                item.get("url") or f"https://www.twitch.tv/videos/{item['id']}",
                item.get("title", ""),
                created + (duration or 0),
                duration,
            )
        )
    return vods


def kick(channel, *, request=http.request):
    reply = request(
        "GET",
        f"https://kick.com/api/v2/channels/{channel.id}/videos",
        headers={"Accept": "application/json"},
    ).json()
    vods = []
    for item in reply if isinstance(reply, list) else reply.get("data", []):
        if item.get("is_live"):
            continue
        video = item.get("video") or {}
        uuid = video.get("uuid")
        if not uuid:
            continue
        duration = (item.get("duration") or 0) / 1000 or None
        created = _timestamp(
            (item.get("start_time") or item.get("created_at") or "").replace(" ", "T")
        )
        vods.append(
            Vod(
                "kick",
                channel.key,
                uuid,
                f"https://kick.com/{channel.id}/videos/{uuid}",
                item.get("session_title", ""),
                created + (duration or 0),
                duration,
            )
        )
    return vods


class Finder:
    """Look up every channel; one failing channel never hides the others."""

    def __init__(self, request=http.request, env=os.environ, youtube_token=None):
        self.request = request
        self.env = env
        self.youtube_token = youtube_token
        self._twitch = None

    def _youtube_auth(self):
        if self.env.get("YOUTUBE_API_KEY"):
            return {"key": self.env["YOUTUBE_API_KEY"]}
        if self.youtube_token:
            return {"token": self.youtube_token()}
        return None

    def find(self, channel):
        if channel.platform == "youtube":
            return youtube(channel, request=self.request, api_auth=self._youtube_auth())
        if channel.platform == "twitch":
            client_id = self.env.get("TWITCH_CLIENT_ID")
            secret = self.env.get("TWITCH_CLIENT_SECRET")
            if not client_id or not secret:
                raise LookupError(
                    "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET が未設定です"
                )
            if self._twitch is None:
                self._twitch = twitch_token(client_id, secret, self.request)
            return twitch(
                channel, client_id=client_id, token=self._twitch, request=self.request
            )
        return kick(channel, request=self.request)
