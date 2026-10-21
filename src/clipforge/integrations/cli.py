"""Lightweight platform CLI; importing it does not load any ML dependencies."""

import argparse
import json
import sys
from pathlib import Path

from ..storage import PipelineError
from . import CREDENTIALS, credential_status
from .clients import Kick, TwitCasting, Twitch, YouTube
from .state import State


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, help="Private persistent SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "credentials", help="Show environment variable names and presence only"
    )
    discover = sub.add_parser(
        "discover", help="Get public metadata/URLs, without downloading video"
    )
    discover.add_argument("platform", choices=list(CREDENTIALS))
    discover.add_argument("channel")
    videos = sub.add_parser(
        "youtube-videos", help="Confirm status of at most 50 known video IDs"
    )
    videos.add_argument("ids", nargs="+")
    videos.add_argument("--daily-budget", type=int, default=1000)
    archive = sub.add_parser(
        "twitch-archive",
        help="Resolve an ended stream by exact stream ID; metadata only",
    )
    archive.add_argument("broadcaster_id")
    archive.add_argument("stream_id", help="ID captured from stream.online")
    subscribe = sub.add_parser(
        "subscribe", help="Register official events; start receiver first"
    )
    subscribe.add_argument("platform", choices=["twitch", "kick", "twitcasting"])
    subscribe.add_argument("broadcaster_id")
    subscribe.add_argument(
        "--callback-url", help="Required for Twitch; other portals store their callback"
    )
    listen = sub.add_parser(
        "serve", help="Receive signed Twitch/Kick events behind an HTTPS reverse proxy"
    )
    listen.add_argument("--host", default="127.0.0.1")
    listen.add_argument("--port", type=int, default=8787)
    events = sub.add_parser(
        "events", help="Read minimal events using a persistent sequence cursor"
    )
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    try:
        if args.command == "credentials":
            result = credential_status()
        else:
            state = State(args.state)
            clients = {
                "twitch": Twitch,
                "kick": Kick,
                "twitcasting": TwitCasting,
                "youtube": YouTube,
            }
            if args.command == "discover":
                result = {
                    "platform": args.platform,
                    "items": clients[args.platform](state).discover(args.channel),
                }
            elif args.command == "twitch-archive":
                result = Twitch(state).resolve_archive(
                    args.broadcaster_id, args.stream_id
                )
            elif args.command == "youtube-videos":
                result = {
                    "platform": "youtube",
                    "items": YouTube(state, daily_budget=args.daily_budget).videos(
                        args.ids
                    ),
                }
            elif args.command == "subscribe":
                if args.platform == "twitch":
                    if not args.callback_url:
                        raise PipelineError("Twitch requires --callback-url")
                    result = Twitch(state).subscribe(
                        args.broadcaster_id, args.callback_url
                    )
                else:
                    if args.callback_url:
                        raise PipelineError(
                            "set this platform's callback in its developer portal"
                        )
                    result = clients[args.platform](state).subscribe(
                        args.broadcaster_id
                    )
            elif args.command == "serve":
                from .webhooks import serve

                if not 1 <= args.port <= 65535:
                    raise PipelineError("port must be between 1 and 65535")
                serve(state, args.host, args.port)
                return 0
            else:
                if args.after < 0 or not 1 <= args.limit <= 1000:
                    raise PipelineError("invalid event cursor or limit")
                result = {"events": state.events(args.after, args.limit)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        detail = (
            str(error) if isinstance(error, PipelineError) else type(error).__name__
        )
        print("ClipForge platforms failed: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
