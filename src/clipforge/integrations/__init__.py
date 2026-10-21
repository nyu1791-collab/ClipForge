"""Official platform metadata APIs. No video downloading or account fallback."""

import os
import re

from ..storage import PipelineError

CREDENTIALS = {
    "twitch": ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"),
    "kick": ("KICK_CLIENT_ID", "KICK_CLIENT_SECRET"),
    "youtube": ("YOUTUBE_API_KEY",),
    "twitcasting": ("TWITCASTING_CLIENT_ID", "TWITCASTING_CLIENT_SECRET"),
}
OPTIONAL = ("TWITCH_EVENTSUB_SECRET",)


def credential(name):
    value = os.environ.get(name, "").strip()
    if not value or value.startswith(("YOUR_", "REPLACE_", "<")):
        raise PipelineError("missing environment variable: " + name)
    if any(c in value for c in "\r\n"):
        raise PipelineError("invalid environment variable: " + name)
    return value


def credential_status():
    def present(name):
        try:
            credential(name)
            return True
        except PipelineError:
            return False

    return {
        "source": "process_environment",
        "remote_authentication_verified": False,
        "platforms": {
            platform: {name: present(name) for name in names}
            for platform, names in CREDENTIALS.items()
        },
        "webhooks": {name: present(name) for name in OPTIONAL},
    }


def identifier(value, pattern=r"[A-Za-z0-9_-]{1,100}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PipelineError("invalid platform identifier")
    return value
