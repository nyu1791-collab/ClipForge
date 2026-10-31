"""python -m clipforge.autopilot <command>

watch       新しい配信を探し、承認を求める／自動で制作を始める（定期実行）
decide      承認ボタンの結果を反映する（--job ID --decision approve|reject|publish|skip|retry|restart）
produce     承認済みジョブを制作し、投稿まで進める（--job ID、--no-publish で制作だけ）
publish     制作済みジョブを投稿する（--job ID --workdir DIR）
analytics   公開済み動画の視聴データを取り込み、話題選定へのメモを更新する
report      今週の振り返りを Discord に送る
check       接続設定の有無だけを表示する（値は表示しない）
preview     いま見つかる配信を表示するだけ（保存・通知・制作なし。キー無しでも YouTube/Kick は確認できる）
auth-youtube  YouTube の refresh token を取得してファイルに保存する（初回だけ、手元のPCで）
"""

import argparse
import http.server
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

from . import config as config_module
from . import youtube
from .jobs import Autopilot
from .state import StateError

GROUPS = {
    "状態の保存": ["AUTOPILOT_GITHUB_TOKEN"],
    "チャンネル設定": ["AUTOPILOT_CONFIG"],
    "Discord（承認ボタン）": [
        "DISCORD_BOT_TOKEN",
        "DISCORD_CHANNEL_ID",
        "DISCORD_OWNER_ID",
    ],
    "Discord（通知だけ）": ["DISCORD_WEBHOOK_URL"],
    "YouTube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
    "YouTube（監視用APIキー・任意）": ["YOUTUBE_API_KEY"],
    "X": ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"],
    "Twitch": ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"],
    "TikTok（下書き）": [
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
        "TIKTOK_REFRESH_TOKEN",
    ],
}


def check(env=os.environ):
    lines = []
    for group, names in GROUPS.items():
        present = [n for n in names if env.get(n)]
        mark = (
            "OK " if len(present) == len(names) else ("一部" if present else "未設定")
        )
        missing = [n for n in names if n not in present]
        lines.append(
            f"[{mark}] {group}"
            + (f"（不足: {', '.join(missing)}）" if missing and present else "")
        )
    try:
        cfg = config_module.load()
        lines.append(
            f"チャンネル {len(cfg.channels)} 件: "
            + "、".join(f"{c.name}({c.platform}/{c.mode})" for c in cfg.channels)
        )
    except config_module.ConfigError as error:
        lines.append(f"チャンネル設定: {error}")
    return lines


def auth_youtube(client_id, client_secret, output, port=8765):
    """Desktop OAuth: open the consent page, catch the code on localhost, save the
    refresh token to a private file. Then: gh secret set YOUTUBE_REFRESH_TOKEN < FILE"""
    redirect = f"http://127.0.0.1:{port}/"
    expected = secrets.token_urlsafe(16)
    caught = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(self.path).query)
            caught.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "認可を受け取りました。このタブは閉じて構いません。".encode()
            )

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = youtube.authorize_url(client_id, redirect, expected)
    print(
        "ブラウザで次のURLを開き、投稿するチャンネルのアカウントで許可してください:\n"
        + url
    )
    webbrowser.open(url)
    thread.join(timeout=600)
    server.server_close()
    if caught.get("state") != expected or "code" not in caught:
        raise SystemExit("認可を受け取れませんでした（タイムアウトまたは拒否）")
    token = youtube.exchange_code(client_id, client_secret, caught["code"], redirect)
    path = Path(output).expanduser()
    path.write_text(token, encoding="utf-8")
    os.chmod(path, 0o600)
    print(
        f"保存しました: {path}\n登録: gh secret set YOUTUBE_REFRESH_TOKEN --repo nyu1791-collab/ClipForge < {path}"
    )
    print("登録後はこのファイルを削除してください。")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipforge.autopilot",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("watch")
    decide = sub.add_parser("decide")
    decide.add_argument("--job", required=True)
    decide.add_argument(
        "--decision",
        required=True,
        choices=["approve", "reject", "publish", "skip", "retry", "restart"],
    )
    produce = sub.add_parser("produce")
    produce.add_argument("--job", required=True)
    produce.add_argument("--workdir", type=Path)
    produce.add_argument(
        "--no-publish", action="store_true", help="制作だけ行い、投稿は publish で行う"
    )
    publish = sub.add_parser("publish")
    publish.add_argument("--job", required=True)
    publish.add_argument("--workdir", type=Path, required=True)
    sub.add_parser("analytics")
    sub.add_parser("report")
    sub.add_parser("check")
    sub.add_parser("preview", help="いま見つかる配信を表示するだけ（保存・通知なし）")
    auth = sub.add_parser("auth-youtube")
    auth.add_argument("--client-id", default=os.environ.get("YOUTUBE_CLIENT_ID"))
    auth.add_argument(
        "--client-secret", default=os.environ.get("YOUTUBE_CLIENT_SECRET")
    )
    auth.add_argument("--output", default="~/youtube-refresh-token.txt")
    args = parser.parse_args(argv)

    if args.command == "check":
        print("\n".join(check()))
        return 0
    if args.command == "auth-youtube":
        if not (args.client_id and args.client_secret):
            raise SystemExit(
                "--client-id と --client-secret（デスクトップアプリのOAuthクライアント）が必要です"
            )
        auth_youtube(args.client_id, args.client_secret, args.output)
        return 0
    if args.command == "preview":
        import tempfile

        from .state import FileBackend, Store

        scratch = Store(FileBackend(Path(tempfile.mkdtemp()) / "state.json"))
        pilot = Autopilot(config_module.load(), scratch, notifier=_Silent())
        print("\n".join(pilot.preview()))
        return 0
    scheduled = args.command in ("watch", "analytics", "report")
    try:
        pilot = Autopilot(config_module.load())
        return _run(pilot, args)
    except (config_module.ConfigError, StateError, youtube.YouTubeError) as error:
        if not scheduled:
            raise
        # Until the owner registers keys, scheduled runs pause instead of failing hourly.
        print(f"::notice::autopilot は未設定のため休止中です: {error}")
        return 0


class _Silent:
    def send(self, *args, **kwargs):
        return None

    def resolve(self, *args, **kwargs):
        return None


def _run(pilot, args):
    if args.command == "watch":
        started = pilot.watch()
        print(f"自動開始 {len(started)} 件")
    elif args.command == "decide":
        pilot.decide(args.job, args.decision)
    elif args.command == "produce":
        return (
            0
            if pilot.produce(args.job, args.workdir, publish=not args.no_publish)
            else 1
        )
    elif args.command == "publish":
        print(pilot.publish_from(args.job, args.workdir))
    elif args.command == "analytics":
        notes = pilot.analytics()
        print(f"メモを更新: {len(notes)} チャンネル")
    elif args.command == "report":
        pilot.report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
