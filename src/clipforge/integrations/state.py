"""Small private SQLite ledger shared by metadata commands and webhook intake."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..storage import PipelineError


def quota_day(now=None):
    return (
        datetime.fromtimestamp(
            time.time() if now is None else now, ZoneInfo("America/Los_Angeles")
        )
        .date()
        .isoformat()
    )


def quota_reset(now=None):
    stamp = datetime.fromtimestamp(
        time.time() if now is None else now, ZoneInfo("America/Los_Angeles")
    )
    return datetime.combine(
        stamp.date() + timedelta(days=1), datetime.min.time(), stamp.tzinfo
    ).timestamp()


class State:
    def __init__(self, path=None):
        root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        self.path = Path(path) if path else root / "clipforge/platforms.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # SQLite must never follow a symlink into another file.
        if self.path.is_symlink():
            raise PipelineError("state database must not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS quota (day TEXT, bucket TEXT, used INTEGER, PRIMARY KEY(day,bucket));
                CREATE TABLE IF NOT EXISTS cooldown (platform TEXT PRIMARY KEY, until REAL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT,
                    event_id TEXT, payload TEXT, received REAL, UNIQUE(platform,event_id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def cached(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM cache WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key, value, seconds):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                (key, json.dumps(value), time.time() + seconds),
            )

    def reserve(self, bucket, cost, limit):
        # Count attempts before the request, including requests rejected upstream.
        day = quota_day()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO quota VALUES (?,?,0)", (day, bucket))
            used = db.execute(
                "SELECT used FROM quota WHERE day=? AND bucket=?", (day, bucket)
            ).fetchone()[0]
            if used + cost > limit:
                raise PipelineError(
                    "local daily API budget exhausted; wait for Pacific midnight"
                )
            db.execute(
                "UPDATE quota SET used=used+? WHERE day=? AND bucket=?",
                (cost, day, bucket),
            )

    def check_cooldown(self, platform):
        with self.connect() as db:
            row = db.execute(
                "SELECT until FROM cooldown WHERE platform=?", (platform,)
            ).fetchone()
        if row and row[0] > time.time():
            raise PipelineError(platform + " API cooldown active; retry later")

    def cool(self, platform, until):
        with self.connect() as db:
            db.execute(
                "INSERT INTO cooldown VALUES (?,?) ON CONFLICT(platform) DO UPDATE SET until=max(until,excluded.until)",
                (platform, until),
            )

    def record(self, platform, event_id, payload):
        with self.connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO events(platform,event_id,payload,received) VALUES (?,?,?,?)",
                (platform, event_id, json.dumps(payload), time.time()),
            )
            return result.rowcount == 1

    def events(self, after=0, limit=100):
        with self.connect() as db:
            rows = db.execute(
                "SELECT sequence,platform,payload FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                (after, limit),
            ).fetchall()
        return [
            {"sequence": seq, "platform": platform, **json.loads(payload)}
            for seq, platform, payload in rows
        ]
