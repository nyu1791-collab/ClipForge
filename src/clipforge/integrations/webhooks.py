"""Signed webhook intake. Persist a minimal event, acknowledge before any media work."""

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..storage import PipelineError
from . import credential, identifier
from .http import Transport


def fresh(value, now=None):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if (
            stamp.tzinfo is None
            or abs((time.time() if now is None else now) - stamp.timestamp()) > 600
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PipelineError("webhook timestamp is missing, stale or invalid") from None


def twitch(headers, body, state):
    message_id = identifier(
        headers.get("twitch-eventsub-message-id"), r"[A-Za-z0-9_-]{1,200}"
    )
    stamp = headers.get("twitch-eventsub-message-timestamp", "")
    fresh(stamp)
    secret = credential("TWITCH_EVENTSUB_SECRET")
    if not 32 <= len(secret) <= 100:
        raise PipelineError("TWITCH_EVENTSUB_SECRET must contain 32-100 characters")
    expected = (
        "sha256="
        + hmac.new(
            secret.encode(), message_id.encode() + stamp.encode() + body, hashlib.sha256
        ).hexdigest()
    )
    if not hmac.compare_digest(
        expected, headers.get("twitch-eventsub-message-signature", "")
    ):
        raise PipelineError("invalid Twitch webhook signature")
    data = json.loads(body)
    kind = headers.get("twitch-eventsub-message-type")
    subscription = data["subscription"]
    event_type = subscription["type"]
    if (
        event_type not in {"stream.online", "stream.offline"}
        or subscription.get("version") != "1"
    ):
        return 204, b""
    if kind == "webhook_callback_verification":
        return 200, data["challenge"].encode()
    if kind == "revocation":
        state.record(
            "twitch",
            message_id,
            {"type": "subscription.revoked", "status": subscription["status"]},
        )
    elif kind == "notification":
        event = data["event"]
        channel = identifier(event["broadcaster_user_id"], r"[0-9]+")
        if channel != subscription.get("condition", {}).get("broadcaster_user_id"):
            raise PipelineError("webhook channel does not match subscription")
        state.record(
            "twitch",
            message_id,
            {
                "type": event_type,
                "channel_id": channel,
                "channel_login": event["broadcaster_user_login"],
                "stream_id": event.get("id"),
                "started_at": event.get("started_at"),
                "event_timestamp": stamp,
                "next_action": "record_stream_identity"
                if event_type == "stream.online"
                else "confirm_offline_then_resolve_matching_archive",
            },
        )
    return 204, b""


def kick(headers, body, state, transport=None):
    message_id = identifier(
        headers.get("kick-event-message-id"), r"[A-Za-z0-9_-]{1,200}"
    )
    stamp = headers.get("kick-event-message-timestamp", "")
    fresh(stamp)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise PipelineError(
            "install ClipForge with the platforms extra for Kick signature verification"
        ) from None
    http = transport or Transport(state)
    public_key = state.cached("kick:public-key")
    if public_key is None:
        public_key = http.request("kick", "https://api.kick.com/public/v1/public-key")[
            "data"
        ]["public_key"]
        state.cache("kick:public-key", public_key, 3600)
    try:
        key = serialization.load_pem_public_key(public_key.encode())
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError
        signature = base64.b64decode(
            headers.get("kick-event-signature", ""), validate=True
        )
        key.verify(
            signature,
            message_id.encode() + b"." + stamp.encode() + b"." + body,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError, TypeError):
        # A forged request must not trigger an unbounded key-refresh request loop.
        # Cache expires hourly; restart with a new state DB only after confirming rotation.
        raise PipelineError(
            "invalid Kick webhook signature or rotated verification key"
        ) from None
    if (
        headers.get("kick-event-type") != "livestream.status.updated"
        or headers.get("kick-event-version") != "1"
    ):
        return 204, b""
    data = json.loads(body)
    if type(data["is_live"]) is not bool:
        raise PipelineError("invalid Kick live status")
    broadcaster = data["broadcaster"]
    state.record(
        "kick",
        message_id,
        {
            "type": "stream.online" if data["is_live"] else "stream.offline",
            "channel_id": str(broadcaster["user_id"]),
            "channel_login": broadcaster["channel_slug"],
            "started_at": data["started_at"],
            "ended_at": data["ended_at"],
            "next_action": "await_creator_supplied_recording",
        },
    )
    return 204, b""


def serve(state, host="127.0.0.1", port=8787):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # No raw paths, headers, bodies or signatures in access logs.

        def do_POST(self):
            routes = {"/webhooks/twitch": twitch, "/webhooks/kick": kick}
            if self.path not in routes:
                self.send_error(404)
                return
            try:
                self.connection.settimeout(10)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 262144 or self.headers.get("Transfer-Encoding"):
                    self.send_error(413)
                    return
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError
                status, reply = routes[self.path](
                    {k.lower(): v for k, v in self.headers.items()}, body, state
                )
            except (PipelineError, ValueError, KeyError, TypeError):
                status, reply = 403, b"Webhook rejected"
            except Exception:
                status, reply = 503, b"Webhook temporarily unavailable"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    # A reverse proxy must terminate HTTPS, enforce rate/connection limits and
    # bind a public :443 callback. This process deliberately runs no FFmpeg/ASR.
    with ThreadingHTTPServer((host, port), Handler) as server:
        print(
            json.dumps({"status": "listening", "host": host, "port": port}), flush=True
        )
        server.serve_forever()
