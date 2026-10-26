"""Attach to a local OpenCode server and stop before its scheduled API retry."""

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def note(message):
    # Progress goes to stderr, which the harness keeps privately; stdout carries events only.
    print(f"opencode_vertex_guard: {message}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    parser.add_argument("--dir", required=True)
    parser.add_argument("prompt")
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    server = subprocess.Popen(
        [args.binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=args.dir,
        stdout=subprocess.DEVNULL,
    )
    client = None
    stopped = threading.Event()
    reason = []
    seen = []
    stream = None

    def stop(message):
        if not stopped.is_set():
            reason.append(message)
            note(message)
            stopped.set()
            print(json.dumps({"type": "retry_stopped", "reason": message}), flush=True)
            if client is not None and client.poll() is None:
                client.kill()
            if server.poll() is None:
                server.kill()

    def watch():
        try:
            for raw in stream:
                if not raw.startswith(b"data: "):
                    continue
                event = json.loads(raw[6:])
                event = event.get("payload", event)
                if not seen:
                    seen.append(event.get("type"))
                    note(f"first server event: {seen[0]}")
                if event.get("type") == "session.status":
                    status = event.get("properties", {}).get("status", {})
                    if status.get("type") == "retry":
                        stop(
                            "OpenCode requested an API retry; stopped without resending"
                        )
                        return
            if not stopped.is_set():
                stop("OpenCode event stream ended; no reconnect")
        except (OSError, ValueError):
            if not stopped.is_set():
                stop("OpenCode event stream failed; no reconnect")

    try:
        # Wait for the local server only. No model request exists before the client starts.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and server.poll() is None:
            try:
                stream = local.open(url + "/event", timeout=180)
                break
            except OSError:
                time.sleep(0.1)
        if stream is None:
            stop("OpenCode server did not start; no model request sent")
            return 1
        note("server event stream connected")
        threading.Thread(target=watch, daemon=True).start()
        if stopped.is_set():
            return 1
        note("starting run")
        client = subprocess.Popen(
            [
                args.binary,
                "run",
                "--attach",
                url,
                "--format",
                "json",
                "--dir",
                args.dir,
                "-m",
                "google-vertex/gemini-3.8-flash",
                args.prompt,
            ],
            cwd=args.dir,
        )
        if stopped.is_set():
            client.kill()
        code = client.wait()
        note(f"run exited with code {code}")
        return 1 if reason else code
    finally:
        stopped.set()
        for child in (client, server):
            if child is not None and child.poll() is None:
                child.kill()
            if child is not None:
                child.wait()
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    sys.exit(main())
