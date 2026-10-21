"""Official metadata clients. Return selected fields rather than credential-bearing bodies."""

import base64
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from ..storage import PipelineError
from . import credential, identifier
from .http import Transport


def callback_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
        or parsed.port not in (None, 443)
    ):
        raise PipelineError(
            "webhook callback must be an HTTPS URL on port 443 without credentials or query"
        )
    return value


class OAuthClient:
    platform = ""
    origin = ""
    token_url = ""

    def __init__(self, state, transport=None):
        self.state = state
        self.http = transport or Transport(state)
        self.token = None
        self.expires = 0

    def headers(self):
        client_id = credential(self.platform.upper() + "_CLIENT_ID")
        if not self.token or time.monotonic() >= self.expires:
            response = self.http.request(
                self.platform,
                self.token_url,
                form={
                    "client_id": client_id,
                    "client_secret": credential(
                        self.platform.upper() + "_CLIENT_SECRET"
                    ),
                    "grant_type": "client_credentials",
                },
            )
            self.token = response["access_token"]
            self.expires = time.monotonic() + max(1, int(response["expires_in"]) - 60)
        headers = {"Authorization": "Bearer " + self.token}
        if self.platform == "twitch":
            headers["Client-Id"] = client_id
        return headers

    def request(self, path, **kwargs):
        return self.http.request(
            self.platform, self.origin + path, headers=self.headers(), **kwargs
        )


class Twitch(OAuthClient):
    platform = "twitch"
    origin = "https://api.twitch.tv/helix"
    token_url = "https://id.twitch.tv/oauth2/token"

    def discover(self, login):
        login = identifier(login, r"[A-Za-z0-9_]{1,25}").lower()
        key = "twitch:archives:" + login
        cached = self.state.cached(key)
        if cached is not None:
            return cached
        users = self.request("/users", params={"login": login})["data"]
        if not users:
            raise PipelineError("Twitch channel not found")
        channel_id = identifier(users[0]["id"], r"[0-9]+")
        videos = self.request(
            "/videos", params={"user_id": channel_id, "type": "archive", "first": 20}
        )["data"]
        results = [
            {
                "video_id": identifier(v["id"], r"[0-9]+"),
                "stream_id": v.get("stream_id"),
                "url": "https://www.twitch.tv/videos/" + v["id"],
                "channel_id": channel_id,
                "title": v["title"],
                "created_at": v["created_at"],
                "duration": v["duration"],
                "status": "archive_listed",  # May still be recording or unavailable.
            }
            for v in videos
        ]
        self.state.cache(key, results, 120)
        return results

    def subscribe(self, broadcaster_id, callback):
        broadcaster_id = identifier(broadcaster_id, r"[0-9]+")
        callback = callback_url(callback)
        secret = credential("TWITCH_EVENTSUB_SECRET")
        if not 32 <= len(secret) <= 100 or not secret.isascii() or not secret.isalnum():
            raise PipelineError(
                "TWITCH_EVENTSUB_SECRET must contain 32-100 ASCII letters/digits"
            )
        # Check existing subscriptions to make repeated setup idempotent. Fail rather
        # than silently ignoring subscriptions beyond an unprocessed page.
        existing, cursor = [], None
        for _ in range(20):
            page = self.request(
                "/eventsub/subscriptions",
                params={"first": 100, **({"after": cursor} if cursor else {})},
            )
            existing.extend(page["data"])
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                break
        if cursor:
            raise PipelineError(
                "subscription pagination limit reached; inspect Twitch subscriptions"
            )
        result = []
        for event in ("stream.online", "stream.offline"):
            found = next(
                (
                    s
                    for s in existing
                    if s["type"] == event
                    and s.get("condition", {}).get("broadcaster_user_id")
                    == broadcaster_id
                    and s.get("transport", {}).get("callback") == callback
                    and s.get("status")
                    in {"enabled", "webhook_callback_verification_pending"}
                ),
                None,
            )
            if found:
                result.append({"event": event, "status": "already_registered"})
                continue
            response = self.request(
                "/eventsub/subscriptions",
                body={
                    "type": event,
                    "version": "1",
                    "condition": {"broadcaster_user_id": broadcaster_id},
                    "transport": {
                        "method": "webhook",
                        "callback": callback,
                        "secret": secret,
                    },
                },
            )
            result.append({"event": event, "status": response["data"][0]["status"]})
        return result


class Kick(OAuthClient):
    platform = "kick"
    origin = "https://api.kick.com/public/v1"
    token_url = "https://id.kick.com/oauth/token"

    def discover(self, slug):
        slug = identifier(slug)
        key = "kick:channel:" + slug
        cached = self.state.cached(key)
        if cached is not None:
            return cached
        channels = self.request("/channels", params={"slug": slug})["data"]
        results = []
        for channel in channels:
            channel_id = int(channel["broadcaster_user_id"])
            results.append(
                {
                    "channel_id": str(channel_id),
                    "url": "https://kick.com/" + slug,
                    "title": channel.get("stream_title", ""),
                    "is_live": (channel.get("livestream") or {}).get("is_live", False),
                    "status": "channel_metadata",
                    "archive_download_available": False,
                }
            )
        # Do not persist stream_url, stream keys, or the full channels response.
        self.state.cache(key, results, 120)
        return results

    def subscribe(self, broadcaster_id):
        broadcaster_id = int(identifier(broadcaster_id, r"[0-9]+"))
        response = self.request(
            "/events/subscriptions",
            body={
                "broadcaster_user_id": broadcaster_id,
                "events": [{"name": "livestream.status.updated", "version": 1}],
                "method": "webhook",
            },
        )
        # Callback is configured in Kick Developer Portal, not in this request.
        return [
            {
                "event": s.get("event"),
                "has_error": bool(s.get("error")),
                "subscription_id": s.get("subscription_id"),
            }
            for s in response.get("data", [])
        ]


class TwitCasting:
    def __init__(self, state, transport=None):
        self.state = state
        self.http = transport or Transport(state)

    def request(self, path, **kwargs):
        pair = (
            credential("TWITCASTING_CLIENT_ID")
            + ":"
            + credential("TWITCASTING_CLIENT_SECRET")
        )
        return self.http.request(
            "twitcasting",
            "https://apiv2.twitcasting.tv" + path,
            headers={
                "X-Api-Version": "2.0",
                "Authorization": "Basic " + base64.b64encode(pair.encode()).decode(),
            },
            **kwargs,
        )

    def discover(self, user):
        user = identifier(user, r"[A-Za-z0-9_:-]{1,100}")
        key = "twitcasting:movies:" + user
        cached = self.state.cached(key)
        if cached is not None:
            return cached
        response = self.request("/users/" + user + "/movies", params={"limit": 20})
        result = [
            {
                "video_id": v["id"],
                "title": v["title"],
                "url": v["link"],
                "is_live": v["is_live"],
                "is_recorded": v["is_recorded"],
                "status": "movie_listed",
            }
            for v in response.get("movies", [])
        ]
        self.state.cache(key, result, 120)
        return result

    def subscribe(self, user_id):
        user_id = identifier(user_id, r"[0-9]+")
        response = self.request(
            "/webhooks", body={"user_id": user_id, "events": ["livestart", "liveend"]}
        )
        return {
            "user_id": response["user_id"],
            "added_events": response["added_events"],
        }


class YouTube:
    def __init__(self, state, transport=None, daily_budget=1000):
        if not 1 <= daily_budget <= 10000:
            raise PipelineError("YouTube daily budget must be between 1 and 10000")
        self.state = state
        self.http = transport or Transport(state)
        self.daily_budget = daily_budget

    def discover(self, channel_id):
        channel_id = identifier(channel_id, r"UC[A-Za-z0-9_-]{22}")
        key = "youtube:rss:" + channel_id
        previous = self.state.cached(key) or {}
        if previous.get("next_fetch", 0) > time.time():
            return previous["videos"]
        headers = {"Accept": "application/atom+xml"}
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]
        status, response_headers, body = self.http.request(
            "youtube",
            "https://www.youtube.com/feeds/videos.xml",
            params={"channel_id": channel_id},
            headers=headers,
            raw=True,
        )
        if status == 304:
            videos = previous.get("videos", [])
        else:
            if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                raise PipelineError("unsupported feed XML")
            try:
                feed = ET.fromstring(body)
                ns = {
                    "atom": "http://www.w3.org/2005/Atom",
                    "yt": "http://www.youtube.com/xml/schemas/2015",
                }
                videos = []
                for entry in feed.findall("atom:entry", ns):
                    video_id = identifier(
                        entry.findtext("yt:videoId", namespaces=ns),
                        r"[A-Za-z0-9_-]{11}",
                    )
                    owner = entry.findtext("yt:channelId", namespaces=ns)
                    if owner != channel_id:
                        raise PipelineError("RSS entry belongs to another channel")
                    videos.append(
                        {
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "url": "https://www.youtube.com/watch?v=" + video_id,
                            "title": entry.findtext("atom:title", "", ns),
                            "status": "discovered_status_unconfirmed",
                        }
                    )
            except ET.ParseError:
                raise PipelineError("invalid YouTube RSS XML") from None
        lower_headers = {k.lower(): v for k, v in response_headers.items()}
        self.state.cache(
            key,
            {
                "videos": videos,
                "etag": lower_headers.get("etag", previous.get("etag")),
                "last_modified": lower_headers.get(
                    "last-modified", previous.get("last_modified")
                ),
                "next_fetch": time.time() + 300,
            },
            86400,
        )
        return videos

    def videos(self, video_ids):
        ids = sorted({identifier(v, r"[A-Za-z0-9_-]{11}") for v in video_ids})
        if not 1 <= len(ids) <= 50:
            raise PipelineError("videos.list requires 1-50 video IDs")
        key = "youtube:videos:" + hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        cached = self.state.cached(key)
        if cached is not None:
            return cached
        api_key = credential("YOUTUBE_API_KEY")
        self.state.check_cooldown("youtube")
        self.state.reserve("youtube_metadata", 1, self.daily_budget)
        response = self.http.request(
            "youtube",
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "id": ",".join(ids),
                "part": "snippet,liveStreamingDetails,status",
                "fields": "items(id,snippet(channelId,title,liveBroadcastContent),liveStreamingDetails(actualStartTime,actualEndTime,scheduledStartTime),status(uploadStatus,privacyStatus))",
            },
            headers={"X-Goog-Api-Key": api_key},
        )
        result = []
        for item in response.get("items", []):
            live = item.get("liveStreamingDetails", {})
            status = (
                "ended"
                if live.get("actualEndTime")
                else "live"
                if live.get("actualStartTime")
                else "scheduled"
                if live.get("scheduledStartTime")
                else "video"
            )
            result.append(
                {
                    "video_id": item["id"],
                    "url": "https://www.youtube.com/watch?v=" + item["id"],
                    "title": item["snippet"]["title"],
                    "channel_id": item["snippet"]["channelId"],
                    "status": status,
                    "actual_start": live.get("actualStartTime"),
                    "actual_end": live.get("actualEndTime"),
                    "upload_status": item.get("status", {}).get("uploadStatus"),
                    "media_download_available": False,
                }
            )
        found = {v["video_id"] for v in result}
        result += [
            {"video_id": v, "status": "unavailable_or_private"}
            for v in ids
            if v not in found
        ]
        self.state.cache(key, result, 300)
        return result
