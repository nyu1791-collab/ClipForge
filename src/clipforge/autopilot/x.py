"""Post to X with API v2 (POST /2/tweets), signed with OAuth 1.0a user context.

Keys come from the X developer portal app ("Keys and tokens", Read and write):
X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET.
Posts are text with a link; X builds the video card from the YouTube URL.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
import urllib.parse

from . import http

TWEETS = "https://api.x.com/2/tweets"
LIMIT = 280
URL_WEIGHT = 23
_URL = re.compile(r"https?://\S+")


class XError(RuntimeError):
    pass


def _quote(value):
    return urllib.parse.quote(str(value), safe="~-._")


def oauth_header(method, url, keys, *, nonce=None, timestamp=None, params=None):
    """RFC 5849 HMAC-SHA1 Authorization header. JSON bodies are not signed."""
    oauth = {
        "oauth_consumer_key": keys["api_key"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(timestamp or time.time())),
        "oauth_token": keys["access_token"],
        "oauth_version": "1.0",
    }
    signed = {**(params or {}), **oauth}
    pairs = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(signed.items()))
    base = "&".join([method.upper(), _quote(url), _quote(pairs)])
    secret = f"{_quote(keys['api_secret'])}&{_quote(keys['access_secret'])}"
    digest = hmac.new(secret.encode(), base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(
        f'{_quote(k)}="{_quote(v)}"' for k, v in sorted(oauth.items())
    )


def keys_from_env(env=os.environ):
    keys = {
        "api_key": env.get("X_API_KEY"),
        "api_secret": env.get("X_API_SECRET"),
        "access_token": env.get("X_ACCESS_TOKEN"),
        "access_secret": env.get("X_ACCESS_SECRET"),
    }
    if not all(keys.values()):
        raise XError(
            "X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET が必要です"
        )
    return keys


def weight(text):
    """X counts every URL as 23 and wide (CJK/full-width) characters as 2."""
    rest = _URL.sub("", text)
    return len(_URL.findall(text)) * URL_WEIGHT + sum(
        2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in rest
    )


def fit(text, url=""):
    """Trim the text (never the URL) so the post fits in 280 weighted characters."""
    suffix = ("\n" + url) if url else ""
    budget = LIMIT - weight(suffix) if suffix else LIMIT
    if weight(text) <= budget:
        return text + suffix
    out = ""
    for char in text:
        if weight(out + char + "…") > budget:
            break
        out += char
    return out.rstrip() + "…" + suffix


def post(text, keys, request=http.request):
    """Create one post. Not retried: a lost reply must not double-post."""
    reply = request(
        "POST",
        TWEETS,
        headers={"Authorization": oauth_header("POST", TWEETS, keys)},
        json_body={"text": text},
    ).json()
    try:
        return reply["data"]["id"]
    except (KeyError, TypeError):
        raise XError(f"投稿IDを受け取れませんでした: {http.redact(reply)}") from None
