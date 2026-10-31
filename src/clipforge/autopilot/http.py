"""Small HTTP helper: JSON in/out, bounded retries for safe reads, redacted errors."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

RETRYABLE = {429, 500, 502, 503, 504}
_SECRET = re.compile(
    r"(access_token|refresh_token|client_secret|token|key|signature)"
    r"([\"']?\s*[:=]\s*[\"']?)[^\"'&\s,}]+",
    re.I,
)


class HttpError(RuntimeError):
    def __init__(self, status, url, body=""):
        self.status = status
        self.body = redact(body)[:500]
        host = urllib.parse.urlsplit(url).netloc
        super().__init__(f"HTTP {status} from {host}: {self.body}")


def redact(text):
    """Remove anything that looks like a credential before it reaches a log."""
    return _SECRET.sub(lambda m: m.group(1) + m.group(2) + "***", str(text or ""))


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def json(self):
        return json.loads(self.body or b"null")

    def text(self):
        return self.body.decode("utf-8", "replace")


def request(
    method,
    url,
    *,
    headers=None,
    params=None,
    json_body=None,
    data=None,
    form=None,
    timeout=60,
    retries=None,
    ok=(200, 201, 204),
    opener=None,
):
    """Send one request. GET/HEAD retry on 429/5xx (3 tries); writes never retry
    here, because a lost reply must not create a duplicate upload or post."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode()
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    headers.setdefault("User-Agent", "clipforge-autopilot/1")
    attempts = (
        retries if retries is not None else (3 if method in ("GET", "HEAD") else 1)
    )
    send = opener or urllib.request.urlopen
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with send(req, timeout=timeout) as reply:
                response = Response(reply.status, dict(reply.headers), reply.read())
        except urllib.error.HTTPError as error:
            response = Response(error.code, dict(error.headers or {}), error.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            raise HttpError(0, url, f"network error: {error}") from None
        if response.status in ok:
            return response
        if response.status in RETRYABLE and attempt + 1 < attempts:
            wait = response.headers.get("retry-after", "")
            time.sleep(min(int(wait), 60) if wait.isdigit() else 2**attempt)
            continue
        raise HttpError(response.status, url, response.text())
    raise HttpError(0, url, "no response")  # pragma: no cover
