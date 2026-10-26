"""Attach to a local OpenCode server and stop before its scheduled API retry."""

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request

# How long the local server may take to open its event stream. Connecting costs nothing;
# no model request exists until the client starts.
SERVER_WAIT_SECONDS = 240


def note(message):
    # Progress goes to stderr, which the harness keeps privately; stdout carries events only.
    print(f"opencode_vertex_guard: {message}", file=sys.stderr, flush=True)


def relay(pipe):
    # The server's own output is kept with the run's private stderr log.
    for line in pipe:
        text = line.decode(errors="replace").rstrip()
        print(f"opencode serve: {text}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--server-wait", type=float, default=SERVER_WAIT_SECONDS)
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
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    threading.Thread(target=relay, args=(server.stdout,), daemon=True).start()
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
        # Only a retry request stops the run. A broken or closed event stream does not.
        try:
            for raw in stream:
                if not raw.startswith(b"data: "):
                    continue
                try:
                    event = json.loads(raw[6:])
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                event = event.get("payload", event)
                if not seen:
                    seen.append(event.get("type"))
                    note(f"first server event: {seen[0]}")
                if event.get("type") == "session.status":
                    status = event.get("properties", {}).get("status", {})
                    if isinstance(status, dict) and status.get("type") == "retry":
                        stop(
                            "OpenCode requested an API retry; stopped without resending"
                        )
                        return
            note("server event stream ended; the run continues without it")
        except (OSError, ValueError) as error:
            note(
                f"server event stream failed ({type(error).__name__}); the run continues"
            )

    try:
        # Wait for the local server only. Each try is short so progress is visible.
        deadline = time.monotonic() + args.server_wait
        attempts = 0
        while stream is None and time.monotonic() < deadline and server.poll() is None:
            attempts += 1
            try:
                stream = local.open(url + "/event", timeout=5)
            except OSError as error:
                if attempts % 12 == 0:
                    note(
                        f"still waiting for the event stream: {type(error).__name__}, try {attempts}"
                    )
                time.sleep(0.5)
        if stream is None:
            stop(
                f"OpenCode server did not start; no model request sent (server exit: {server.poll()})"
            )
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
