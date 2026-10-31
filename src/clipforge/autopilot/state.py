"""Durable autopilot state: seen VODs, jobs and their status, published videos.

Two backends share one JSON document:
- FileBackend: a local file (development, the control VM).
- VariableBackend: a private GitHub Actions repository variable (``AUTOPILOT_STATE``),
  readable only by collaborators, so the public repository never shows the
  channel list or the job history.

Every change goes through ``update(fn)``: reload, apply, save. Status changes go
through ``transition``, which refuses moves that could repeat paid or public work
(for example starting a second production for the same VOD).
"""

import json
import os
import time
import urllib.parse
from pathlib import Path

from . import http

VERSION = 1
# A GitHub variable holds at most 48 KB; keep headroom for the JSON envelope.
MAX_BYTES = 46_000
JOB_TTL = 21 * 86400
SEEN_TTL = 45 * 86400
VIDEO_TTL = 120 * 86400
HISTORY = 8

TRANSITIONS = {
    "awaiting_approval": {"approved", "rejected", "expired"},
    "approved": {"producing", "rejected"},
    "producing": {"produced", "failed"},
    "produced": {"awaiting_publish", "published", "failed"},
    "awaiting_publish": {"published", "skipped", "failed"},
    "failed": {"approved", "rejected"},
}
TERMINAL = {"rejected", "expired", "published", "skipped"}


class StateError(RuntimeError):
    pass


def empty():
    return {
        "version": VERSION,
        "seen": {},
        "bootstrapped": {},
        "jobs": {},
        "videos": {},
        "slots": [],
        "health": {},
    }


class FileBackend:
    def __init__(self, path):
        self.path = Path(path).expanduser()

    def read(self):
        if not self.path.is_file():
            return None
        return self.path.read_text(encoding="utf-8")

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


class VariableBackend:
    """GitHub Actions repository variable. Needs a token with Variables read/write."""

    def __init__(
        self, repository, token, name="AUTOPILOT_STATE", requester=http.request
    ):
        if not repository or not token:
            raise StateError("AUTOPILOT_GITHUB_TOKEN と GITHUB_REPOSITORY が必要です")
        self.base = f"https://api.github.com/repos/{repository}/actions/variables"
        self.name = name
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.request = requester

    def read(self):
        try:
            reply = self.request(
                "GET",
                f"{self.base}/{urllib.parse.quote(self.name)}",
                headers=self.headers,
            )
        except http.HttpError as error:
            if error.status == 404:
                return None
            raise
        return reply.json().get("value")

    def write(self, text):
        body = {"name": self.name, "value": text}
        try:
            self.request(
                "PATCH",
                f"{self.base}/{urllib.parse.quote(self.name)}",
                headers=self.headers,
                json_body=body,
            )
        except http.HttpError as error:
            if error.status != 404:
                raise
            self.request("POST", self.base, headers=self.headers, json_body=body)


def backend_from_env():
    if os.environ.get("AUTOPILOT_STATE_BACKEND") == "variable":
        return VariableBackend(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("AUTOPILOT_GITHUB_TOKEN", ""),
        )
    default = Path.home() / ".clipforge-autopilot" / "state.json"
    return FileBackend(os.environ.get("AUTOPILOT_STATE_FILE") or default)


class Store:
    def __init__(self, backend, clock=time.time):
        self.backend = backend
        self.clock = clock

    def load(self):
        text = self.backend.read()
        if not text:
            return empty()
        data = json.loads(text)
        if data.get("version") != VERSION:
            raise StateError(f"未対応の状態バージョンです: {data.get('version')}")
        for key, value in empty().items():
            data.setdefault(key, value)
        return data

    def save(self, data):
        prune(data, self.clock())
        text = json.dumps(
            data, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        while len(text.encode()) > MAX_BYTES and _drop_oldest_terminal(data):
            text = json.dumps(
                data, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        if len(text.encode()) > MAX_BYTES:
            raise StateError(
                "状態が大きすぎます。完了済みの古いジョブを整理してください"
            )
        self.backend.write(text)
        return data

    def update(self, change):
        """Reload, apply ``change(data)``, save. Returns whatever ``change`` returns."""
        data = self.load()
        result = change(data)
        self.save(data)
        return result


def prune(data, now):
    data["seen"] = {k: t for k, t in data["seen"].items() if now - t < SEEN_TTL}
    data["jobs"] = {
        k: j
        for k, j in data["jobs"].items()
        if j.get("status") not in TERMINAL or now - j.get("updated_at", now) < JOB_TTL
    }
    data["videos"] = {
        k: v
        for k, v in data["videos"].items()
        if now - v.get("published_at", now) < VIDEO_TTL
    }
    data["slots"] = sorted(s for s in data["slots"] if s > now - 86400)


def _drop_oldest_terminal(data):
    finished = sorted(
        (j.get("updated_at", 0), k)
        for k, j in data["jobs"].items()
        if j.get("status") in TERMINAL
    )
    if finished:
        del data["jobs"][finished[0][1]]
        return True
    if data["videos"]:
        oldest = min(
            data["videos"], key=lambda k: data["videos"][k].get("published_at", 0)
        )
        del data["videos"][oldest]
        return True
    return False


def transition(job, status, now, note=""):
    current = job.get("status")
    if status not in TRANSITIONS.get(current, set()):
        raise StateError(
            f"ジョブ {job.get('id')} は {current} から {status} にできません"
        )
    job["status"] = status
    job["updated_at"] = now
    job.setdefault("history", []).append([round(now), status, str(note)[:160]])
    job["history"] = job["history"][-HISTORY:]
    return job
