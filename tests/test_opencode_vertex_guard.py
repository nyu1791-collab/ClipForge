import json
import os
from pathlib import Path
import subprocess
import sys

GUARD = Path(__file__).resolve().parents[1] / "tools" / "opencode_vertex_guard.py"

# A stand-in for `opencode serve` and `opencode run`; it makes no model request.
FAKE_OPENCODE = """#!PYTHON_PATH
import http.server
import json
import os
import sys
import time

arguments = sys.argv[1:]
mode = os.environ["FAKE_MODE"]
if arguments[0] == "serve":
    if mode == "silent":
        time.sleep(60)
    port = int(arguments[arguments.index("--port") + 1])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if mode == "malformed":
                self.wfile.write(b"data: {not json\\n\\n")
            busy = {"type": "session.status", "properties": {"status": {"type": "busy"}}}
            self.wfile.write(b"data: " + json.dumps(busy).encode() + b"\\n\\n")
            self.wfile.flush()
            if mode == "retry":
                retry = {"type": "session.status", "properties": {"status": {"type": "retry"}}}
                self.wfile.write(b"data: " + json.dumps(retry).encode() + b"\\n\\n")
                self.wfile.flush()
            time.sleep(60)

        def log_message(self, *arguments):
            pass

    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
time.sleep(10 if mode == "retry" else 0)
print(json.dumps({"type": "text", "part": {"text": "done"}}), flush=True)
"""


def run_guard(tmp_path, mode, *options):
    fake = tmp_path / "opencode"
    fake.write_text(
        FAKE_OPENCODE.replace("PYTHON_PATH", sys.executable), encoding="utf-8"
    )
    fake.chmod(0o755)
    environment = dict(os.environ, FAKE_MODE=mode)
    return subprocess.run(
        [
            sys.executable,
            str(GUARD),
            str(fake),
            "--dir",
            str(tmp_path),
            *options,
            "synthetic prompt",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        check=False,
    )


def test_run_completes_and_stdout_carries_only_events(tmp_path):
    completed = run_guard(tmp_path, "ok")

    assert completed.returncode == 0
    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert events == [{"type": "text", "part": {"text": "done"}}]
    assert "first server event: session.status" in completed.stderr
    assert "run exited with code 0" in completed.stderr


def test_retry_request_stops_the_run_without_resending(tmp_path):
    completed = run_guard(tmp_path, "retry")

    assert completed.returncode == 1
    stopped = json.loads(completed.stdout.splitlines()[-1])
    assert stopped["type"] == "retry_stopped"
    assert "requested an API retry" in stopped["reason"]


def test_malformed_event_line_does_not_stop_the_run(tmp_path):
    completed = run_guard(tmp_path, "malformed")

    assert completed.returncode == 0
    assert json.loads(completed.stdout.splitlines()[0])["type"] == "text"
    assert "failed" not in completed.stderr


def test_server_that_never_listens_stops_before_any_model_request(tmp_path):
    completed = run_guard(tmp_path, "silent", "--server-wait", "2")

    assert completed.returncode == 1
    stopped = json.loads(completed.stdout.splitlines()[-1])
    assert "did not start; no model request sent" in stopped["reason"]
    assert "server exit: None" in stopped["reason"]
