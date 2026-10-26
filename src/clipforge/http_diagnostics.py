"""Bounded private provider errors; never record authorization headers."""

import os
import re

from .storage import write_json


def capture_http_error(error, path):
    body = error.read(65536).decode("utf-8", errors="replace")
    token = os.environ.get("CLIPFORGE_VERTEX_ACCESS_TOKEN")
    if token:
        body = body.replace(token, "[REDACTED]")
    body = re.sub(r"(?i)Bearer\s+[^\s\"',;]+", "Bearer [REDACTED]", body)
    headers = {
        name: error.headers[name]
        for name in (
            "retry-after",
            "date",
            "content-type",
            "x-goog-request-id",
            "x-request-id",
            "x-cloud-trace-context",
            "x-guploader-uploadid",
        )
        if error.headers and error.headers.get(name)
    }
    write_json(path, {"http_status": error.code, "headers": headers, "body": body})
    return {
        "http_status": error.code,
        "http_error_file": path.name,
        "retry_after": headers.get("retry-after"),
    }
