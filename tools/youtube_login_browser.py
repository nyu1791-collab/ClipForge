"""Temporary human-operated YouTube browser; export only YouTube cookies privately."""

import hashlib
import hmac
import http.client
from http.cookiejar import MozillaCookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode


def main():
    os.umask(0o077)
    root_token = os.environ["YOUTUBE_LOGIN_TOKEN"].encode()
    if len(root_token) < 32:
        raise RuntimeError("Protected login session is not configured")
    ui = hmac.new(root_token, b"ui", hashlib.sha256).hexdigest()
    export = hmac.new(root_token, b"export", hashlib.sha256).hexdigest()
    finished = threading.Event()
    lock = threading.Lock()
    state = {"cookies": None}
    work = Path(tempfile.mkdtemp(prefix="clipforge-login-"))
    profile = work / "chrome"
    processes = []

    class Silent:
        def debug(self, *a, **k):
            pass

        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    def capture():
        from yt_dlp import YoutubeDL

        with YoutubeDL(
            {
                "cookiesfrombrowser": ("chrome", str(profile), None, None),
                "quiet": True,
                "no_warnings": True,
                "logger": Silent(),
            }
        ) as ydl:
            cookies = [
                c
                for c in ydl.cookiejar
                if (
                    c.domain.lstrip(".").lower() == "youtube.com"
                    or c.domain.lstrip(".").lower().endswith(".youtube.com")
                )
                and (not c.expires or c.expires > time.time())
            ]
        if not any(
            c.name in {"LOGIN_INFO", "SID", "__Secure-1PSID", "__Secure-3PSID"}
            for c in cookies
        ):
            raise RuntimeError("YouTube login cookies are not ready")
        cookiefile = work / "cookies.txt"
        jar = MozillaCookieJar(str(cookiefile))
        for c in cookies:
            jar.set_cookie(c)
        jar.save(ignore_discard=True, ignore_expires=True)
        cookiefile.chmod(0o600)
        data = cookiefile.read_bytes()
        if not 0 < len(data) <= 36000:
            raise RuntimeError("Cookie payload size invalid")
        with lock:
            state["cookies"] = data

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, status, data, kind="text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path == "/" + ui + "/complete":
                try:
                    capture()
                    self.reply(
                        200,
                        "認証情報を保護して登録中です。この画面を閉じてチャットで「ログイン完了」と伝えてください。".encode(),
                    )
                except Exception:
                    self.reply(
                        409,
                        "YouTubeへのログイン完了後にもう一度「ログイン完了」を押してください。認証情報は表示していません。".encode(),
                    )
            elif self.path == "/" + export + "/finish":
                self.reply(200, b"finished")
                finished.set()
            else:
                self.reply(404, b"Not found")

        def do_GET(self):
            if self.path == "/" + export + "/cookies":
                with lock:
                    data = state["cookies"]
                return self.reply(
                    200 if data else 202, data or b"waiting", "application/octet-stream"
                )
            prefix = "/" + ui + "/"
            if not self.path.startswith(prefix):
                return self.reply(404, b"Not found")
            relative = "/" + self.path[len(prefix) :]
            if relative == "/":
                query = urlencode(
                    {
                        "autoconnect": "true",
                        "resize": "scale",
                        "path": ui + "/websockify",
                    }
                )
                page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ClipForge YouTubeログイン</title><h3>YouTubeへ本人がログインする一時ブラウザ</h3><p>下のChromeの実画面でGoogle/YouTubeへログインしてください。パスワードをチャットへ送らないでください。</p><form method="post" action="/{ui}/complete"><button style="font-size:20px">ログイン完了</button></form><iframe style="width:100%;height:78vh;border:1px solid #aaa" src="/{ui}/vnc.html?{query}"></iframe>"""
                return self.reply(200, page.encode())
            if self.headers.get("Upgrade", "").lower() == "websocket":
                backend = socket.create_connection(("127.0.0.1", 6080), timeout=10)
                try:
                    headers = (
                        "GET "
                        + relative
                        + " HTTP/1.1\r\n"
                        + "".join(
                            k + ": " + v + "\r\n" for k, v in self.headers.items()
                        )
                        + "\r\n"
                    )
                    backend.sendall(headers.encode())
                    while not finished.is_set():
                        ready, _, _ = select.select(
                            [self.connection, backend], [], [], 2
                        )
                        for src in ready:
                            data = src.recv(65536)
                            if not data:
                                return
                            (
                                backend if src is self.connection else self.connection
                            ).sendall(data)
                finally:
                    backend.close()
            else:
                backend = http.client.HTTPConnection("127.0.0.1", 6080, timeout=20)
                try:
                    backend.request("GET", relative)
                    r = backend.getresponse()
                    self.reply(
                        r.status,
                        r.read(),
                        r.getheader("Content-Type", "application/octet-stream"),
                    )
                finally:
                    backend.close()

    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def start(command, **kw):
        p = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw
        )
        processes.append(p)
        return p

    try:
        start(["Xvfb", ":99", "-screen", "0", "1280x900x24", "-nolisten", "tcp"])
        time.sleep(2)
        env = {**os.environ, "DISPLAY": ":99"}
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            raise RuntimeError("A stock Chrome browser is required")
        start(
            [
                chrome,
                "--no-first-run",
                "--no-default-browser-check",
                "--user-data-dir=" + str(profile),
                "--window-size=1280,850",
                "https://www.youtube.com/account",
            ],
            env=env,
        )
        start(
            [
                "x11vnc",
                "-display",
                ":99",
                "-localhost",
                "-nopw",
                "-forever",
                "-shared",
                "-rfbport",
                "5900",
                "-noclipboard",
            ],
            env=env,
        )
        start(
            ["websockify", "--web=/usr/share/novnc", "127.0.0.1:6080", "127.0.0.1:5900"]
        )
        tunnel_log = work / "tunnel.log"
        with tunnel_log.open("w") as log:
            tunnel = subprocess.Popen(
                [
                    os.environ["CLOUDFLARED_BIN"],
                    "tunnel",
                    "--url",
                    "http://127.0.0.1:8765",
                    "--no-autoupdate",
                ],
                stdout=log,
                stderr=log,
            )
            processes.append(tunnel)
            base = None
            for _ in range(60):
                found = re.search(
                    r"https://[a-z0-9-]+\.trycloudflare\.com", tunnel_log.read_text()
                )
                if found:
                    base = found.group()
                    break
                if tunnel.poll() is not None:
                    break
                time.sleep(1)
            if not base:
                raise RuntimeError("Temporary HTTPS endpoint did not start")
            info = work / "session-info.json"
            info.write_text(
                json.dumps(
                    {
                        "base_url": base,
                        "expires_after_sec": 1200,
                        "run_id": os.environ["GITHUB_RUN_ID"],
                    }
                )
            )
            subprocess.run(
                [
                    "gh",
                    "release",
                    "create",
                    "clipforge-youtube-login-" + os.environ["GITHUB_RUN_ID"],
                    str(info),
                    "--repo",
                    "nyu1791-collab/ai-model-workers",
                    "--latest=false",
                    "--title",
                    "Temporary YouTube login",
                    "--notes",
                    "Endpoint metadata only. No Google credentials or cookies.",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                summary.write(
                    "### YouTubeログイン\n\n[一時ブラウザを開く]("
                    + base
                    + "/"
                    + ui
                    + "/)\n\n20分で終了します。本人がログイン後、上部の「ログイン完了」を押してください。\n"
                )
            print(
                "YOUTUBE_LOGIN_BROWSER=ready; open the private Actions summary; no credentials printed",
                flush=True,
            )
            if not finished.wait(1200):
                raise RuntimeError("Login session expired; no automatic restart")
            print("YOUTUBE_LOGIN_BROWSER=completed", flush=True)
    finally:
        finished.set()
        server.shutdown()
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
        for p in reversed(processes):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
