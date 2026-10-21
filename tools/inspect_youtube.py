"""One metadata-only diagnosis; never prints remote bodies or generates video."""

import json
import os
from pathlib import Path
import subprocess

from url_common import WORKER, youtube_url
from url_runner import download_command


def classify(stderr):
    message = stderr.decode("utf-8", errors="replace").lower()
    for code, patterns in [
        (
            "bot_confirmation_required",
            ("not a bot", "confirm you’re not", "confirm you're not"),
        ),
        ("http_403", ("http error 403", "http error: 403")),
        ("http_429", ("http error 429", "http error: 429")),
        (
            "authentication_required",
            ("sign in", "login required", "private video", "age-restricted"),
        ),
        ("cli_option_error", ("no such option", "unrecognized arguments")),
        ("download_dependency_missing", ("no module named",)),
        (
            "unavailable_video",
            ("video unavailable", "video is unavailable", "not available"),
        ),
        (
            "network_failure",
            ("timed out", "unable to download", "certificate verify failed"),
        ),
    ]:
        if any(pattern in message for pattern in patterns):
            return code
    return "unclassified_extractor_failure"


def main():
    if os.environ.get("GITHUB_REPOSITORY") != WORKER:
        raise RuntimeError("Use designated private runner")
    url = youtube_url(os.environ["CLIPFORGE_VIDEO_URL"])
    output = Path("artifacts/url-inspection.json")
    output.parent.mkdir(exist_ok=True)
    result = {
        "generation_requests": 0,
        "video_download_requests": 0,
        "metadata_attempts": 1,
        "automatic_retry": False,
    }
    try:
        response = subprocess.run(
            download_command(url, Path("unused"), metadata=True),
            capture_output=True,
            timeout=180,
        )
        result.update(
            exit_code=response.returncode, metadata_bytes=len(response.stdout)
        )
        if response.returncode:
            result.update(status="failed", reason=classify(response.stderr))
        elif len(response.stdout) > 8 * 1024**2:
            result.update(status="failed", reason="metadata_size_limit")
        else:
            data = json.loads(response.stdout)
            result.update(
                status="metadata_obtained",
                duration=data.get("duration"),
                availability=data.get("availability"),
                live_status=data.get("live_status"),
                format_count=len(data.get("formats", [])),
            )
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__)
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
