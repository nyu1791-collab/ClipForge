"""Shared `gh` command runner for the ClipForge tools and worker.

Callers choose their own timeout, failure exception type and failure message.
Failure messages never include stderr: gh may echo signed URLs or tokens there.
Only the HTTP status code can be added, and only when the caller asks for it.
"""

import re
import subprocess

DEFAULT_TIMEOUT = 120


def run(
    *args,
    body=None,
    timeout=DEFAULT_TIMEOUT,
    error_type=RuntimeError,
    message="GitHub operation failed",
    include_command=False,
    include_http_status=False,
):
    """Run `gh` with args, send body on stdin, and return stdout.

    On a non-zero exit, raise error_type(message). With include_command, the
    first argument (the gh subcommand) is appended after ": ". With
    include_http_status, " (HTTP nnn)" is appended when stderr names a status.
    A subprocess.TimeoutExpired from timeout is not converted.
    """
    result = subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        text = message
        if include_command:
            text += ": " + args[0]
        if include_http_status:
            status = re.search(r"HTTP ([0-9]{3})\b", result.stderr)
            if status:
                text += " (HTTP " + status[1] + ")"
        raise error_type(text)
    return result.stdout
