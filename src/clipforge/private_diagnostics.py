"""Bound and redact diagnostic excerpts before storing them in private results."""

import os
import re


def sanitized_tail(value, *, environment=None, lines=50):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return []
    text = "\n".join(value.splitlines()[-lines:])[-100_000:]
    # Never persist credentials or signed URLs, even in a private result bundle.
    secrets = {**os.environ, **(environment or {})}
    for key, secret in secrets.items():
        if (
            re.search(r"token|password|secret|credential|cookie|api.?key", key, re.I)
            and isinstance(secret, str)
            and len(secret) >= 8
        ):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)https?://[^\s\"'<>]+", "[URL REDACTED]", text)
    text = re.sub(r"(?i)bearer\s+[^\s\"',;]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(token|password|secret|api[_-]?key|authorization|cookie)"
        r"([\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+",
        r"\1\2[REDACTED]",
        text,
    )
    return [line[:2000] for line in text.splitlines()[-lines:]]
