#!/usr/bin/env python3
"""Inspect one public Kick VOD; never retry denials or expose playback URLs."""

import argparse
import json
import re
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, limit):
    # Inherit proxy and CA configuration. No cookies or platform API credentials.
    try:
        with build_opener(NoRedirect()).open(
            Request(url, headers={"User-Agent": "ClipForge/0.3"}), timeout=20
        ) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                return {"status": "response_too_large"}, None
            return {"status": "ok", "http_status": response.status}, body
    except HTTPError as error:
        body = error.read(4096)
        result = {"status": "http_error", "http_status": error.code}
        if re.search(rb"error code:\s*1010\b", body):
            result["reason"] = "client_signature_denied_1010"
        return result, None


class VideoMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.collect = False
        self.parts = []
        self.videos = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.collect = dict(attrs).get("type") == "application/ld+json"
            self.parts = []

    def handle_data(self, data):
        if self.collect:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.collect:
            self.collect = False
            try:
                document = json.loads("".join(self.parts))
            except ValueError:
                return
            if not isinstance(document, dict):
                return
            nodes = document.get("@graph", [document])
            if isinstance(nodes, list):
                self.videos.extend(
                    node
                    for node in nodes
                    if isinstance(node, dict) and node.get("@type") == "VideoObject"
                )


def inspect(url):
    if not re.fullmatch(
        r"https://kick\.com/[A-Za-z0-9_-]+/videos/[0-9a-fA-F]{8}"
        r"(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        url,
    ):
        raise ValueError("expected a canonical HTTPS Kick VOD URL")
    page, body = fetch(url, 2 * 1024 * 1024)
    result = {"url": url, "page": page, "video_downloaded": False}
    if body is None:
        return result
    parser = VideoMetadata()
    parser.feed(body.decode("utf-8", errors="replace"))
    matches = [v for v in parser.videos if v.get("url") == url]
    if len(matches) != 1:
        result["metadata_status"] = "missing_or_ambiguous"
        return result
    video = matches[0]
    result["metadata"] = {
        k: video[k] for k in ("name", "uploadDate", "duration", "expires") if k in video
    }
    media_url = video.get("contentUrl", "")
    parsed = urlsplit(media_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "stream.kick.com"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(".m3u8")
    ):
        result["playlist"] = {"status": "unsupported_media_url"}
        return result
    playlist, content = fetch(media_url, 256 * 1024)
    result["playlist"] = playlist
    if content is not None:
        playlist["valid_hls"] = content.lstrip().startswith(b"#EXTM3U")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        result = inspect(args.url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("playlist", {}).get("valid_hls") else 2
    except Exception as error:
        # Never dump exception URLs, which could contain playback credentials.
        print(
            json.dumps(
                {"status": "inspection_failed", "error_type": type(error).__name__}
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
