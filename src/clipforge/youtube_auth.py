"""Private, short-lived cookie copies for YouTube acquisition."""

import base64
import binascii
import os
from pathlib import Path
import stat
import tempfile

from .storage import PipelineError

MAX_COOKIE_BYTES = 1024 * 1024


def cookie_copy(cookies_file=None):
    """Return (temporary directory, path); explicit file takes precedence over env.

    yt-dlp writes cookie jars on close, so never give it the user's original.
    Errors intentionally omit filenames, cookie values and parser diagnostics.
    """
    encoded = os.environ.get("YOUTUBE_COOKIES_B64", "")
    if cookies_file is None and not encoded:
        return None, None
    try:
        if cookies_file is not None:
            descriptor = os.open(
                cookies_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError
                data = handle.read(MAX_COOKIE_BYTES + 1)
        else:
            if len(encoded) > 4 * MAX_COOKIE_BYTES // 3 + 4:
                raise ValueError
            data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > MAX_COOKIE_BYTES:
            raise ValueError
        content = data.decode("utf-8")
        if not content.startswith(
            ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
        ):
            raise ValueError
        rows = [
            line
            for line in content.splitlines()
            if line and (not line.startswith("#") or line.startswith("#HttpOnly_"))
        ]
        if not rows:
            raise ValueError
        for row in rows:
            fields = row.removeprefix("#HttpOnly_").split("\t")
            if (
                len(fields) != 7
                or fields[1] not in {"TRUE", "FALSE"}
                or fields[3] not in {"TRUE", "FALSE"}
            ):
                raise ValueError
            int(fields[4])
            domain = fields[0].lstrip(".").lower()
            if not any(
                domain == host or domain.endswith("." + host)
                for host in ("youtube.com", "google.com")
            ):
                raise ValueError
    except (OSError, ValueError, UnicodeError, binascii.Error):
        raise PipelineError(
            "invalid YouTube cookies: expected a Netscape file containing only YouTube/Google cookies (maximum 1 MiB)"
        ) from None
    temporary = tempfile.TemporaryDirectory(prefix="clipforge-youtube-auth-")
    try:
        path = Path(temporary.name) / "cookies.txt"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return temporary, str(path)
    except BaseException:
        temporary.cleanup()
        raise
