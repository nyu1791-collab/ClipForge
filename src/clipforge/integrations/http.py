"""Bounded official API requests; never expose URLs, tokens or response bodies."""

import json
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..storage import PipelineError
from .state import quota_reset

HOSTS = {
    "twitch": {"id.twitch.tv", "api.twitch.tv"},
    "kick": {"id.kick.com", "api.kick.com"},
    "youtube": {"www.googleapis.com", "www.youtube.com"},
    "twitcasting": {"apiv2.twitcasting.tv"},
}
MAX_RESPONSE = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Credentials must not be forwarded to a redirect target.


def retry_at(headers):
    now = time.time()
    value = headers.get("Retry-After", "")
    try:
        return now + max(1, int(value))
    except ValueError:
        try:
            return max(now + 1, parsedate_to_datetime(value).timestamp())
        except (TypeError, ValueError, OverflowError):
            return now + 300


class Transport:
    def __init__(self, state):
        self.state = state
        # Inherits the managed environment's proxy and trusted CA configuration.
        self.opener = build_opener(NoRedirect())

    def request(
        self,
        platform,
        url,
        *,
        params=None,
        form=None,
        body=None,
        headers=None,
        method=None,
        raw=False,
    ):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in HOSTS[platform]
            or parsed.username
            or parsed.port not in (None, 443)
        ):
            raise PipelineError("only official HTTPS API hosts are allowed")
        cooldown = (
            "youtube:rss"
            if platform == "youtube" and parsed.hostname == "www.youtube.com"
            else platform
        )
        self.state.check_cooldown(cooldown)
        request_headers = {"Accept": "application/json", "User-Agent": "ClipForge/0.3"}
        request_headers.update(headers or {})
        if params:
            url += "?" + urlencode(params, doseq=True)
        data = None
        if form is not None:
            data = urlencode(form).encode()
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(
                Request(url, data=data, headers=request_headers, method=method),
                timeout=20,
            ) as response:
                content = response.read(MAX_RESPONSE + 1)
                if len(content) > MAX_RESPONSE:
                    raise PipelineError(platform + " API response exceeds size limit")
                result = (response.status, dict(response.headers), content)
        except HTTPError as error:
            if error.code == 304 and raw:
                return 304, dict(error.headers), b""
            reason = None
            try:
                payload = json.loads(error.read(MAX_RESPONSE))
                reason = payload.get("error", {}).get("errors", [{}])[0].get("reason")
            except (ValueError, AttributeError, IndexError, TypeError):
                pass
            if platform == "youtube" and reason in {
                "quotaExceeded",
                "dailyLimitExceeded",
            }:
                self.state.cool(platform, quota_reset())
                raise PipelineError(
                    "YouTube quota exhausted; requests paused until Pacific midnight"
                ) from None
            if error.code == 429 or reason in {
                "rateLimitExceeded",
                "userRateLimitExceeded",
            }:
                self.state.cool(cooldown, retry_at(error.headers))
            elif error.code >= 500:
                self.state.cool(cooldown, time.time() + 60)
            # No retries of subscription creation, authentication failures, or denials.
            raise PipelineError(
                f"{platform} API returned HTTP {error.code}; request was not retried"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise PipelineError(
                platform + " API network failure; request was not retried"
            ) from None
        if raw:
            return result
        try:
            return json.loads(result[2])
        except ValueError:
            raise PipelineError(platform + " API returned invalid JSON") from None
