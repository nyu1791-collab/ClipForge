import json
import subprocess
import time
from pathlib import Path

import pytest

from clipforge.autopilot import config as cfgmod
from clipforge.autopilot import (
    http,
    jobs,
    metadata,
    notify,
    sources,
    state,
    tiktok,
    x,
    youtube,
)
from clipforge.autopilot.__main__ import check

NOW = 1_800_000_000.0  # 2027-01-15


class Reply(http.Response):
    def __init__(self, status=200, body=None, headers=None):
        raw = (
            body
            if isinstance(body, bytes)
            else json.dumps(body if body is not None else {}).encode()
        )
        super().__init__(status, headers or {}, raw)


class FakeHTTP:
    """Route by (method, URL prefix); record every call."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def on(self, method, prefix, *replies):
        self.routes.append([method, prefix, list(replies)])
        return self

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for route in self.routes:
            if route[0] == method and url.startswith(route[1]):
                reply = route[2][0] if len(route[2]) == 1 else route[2].pop(0)
                if isinstance(reply, Exception):
                    raise reply
                if callable(reply):
                    reply = reply(url, kwargs)
                if reply.status not in kwargs.get("ok", (200, 201, 204)):
                    raise http.HttpError(reply.status, url, reply.text())
                return reply
        raise AssertionError(f"unexpected {method} {url}")

    def called(self, method, prefix):
        return [c for c in self.calls if c[0] == method and c[1].startswith(prefix)]


CONFIG = """
channels:
  - {platform: youtube, id: UCabcdefghijklmnopqrstuv, name: Alice, mode: approve, publish: [youtube, x]}
  - {platform: twitch, id: bob_live, name: Bob, mode: auto, publish_mode: auto, publish: [youtube]}
publish_hours: [12, 18]
"""


def make_store(tmp_path, clock=lambda: NOW):
    return state.Store(state.FileBackend(tmp_path / "state.json"), clock=clock)


# ---------------------------------------------------------------- config
def test_config_parses_and_validates():
    cfg = cfgmod.parse(CONFIG)
    assert [c.key for c in cfg.channels] == [
        "youtube:ucabcdefghijklmnopqrstuv",
        "twitch:bob_live",
    ]
    assert cfg.publish_hours == (12, 18)
    with pytest.raises(cfgmod.ConfigError, match="UC"):
        cfgmod.parse('{"channels": [{"platform": "youtube", "id": "bad"}]}')
    with pytest.raises(cfgmod.ConfigError, match="mode"):
        cfgmod.parse(
            '{"channels": [{"platform": "kick", "id": "abc", "mode": "maybe"}]}'
        )
    with pytest.raises(cfgmod.ConfigError, match="2回"):
        cfgmod.parse(
            '{"channels": [{"platform": "kick", "id": "abc"}, {"platform": "kick", "id": "abc"}]}'
        )


def test_check_lists_presence_without_values(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG", CONFIG)
    lines = check({"YOUTUBE_CLIENT_ID": "secret-value", "X_API_KEY": "k"})
    joined = "\n".join(lines)
    assert "secret-value" not in joined
    assert "[一部] YouTube" in joined and "チャンネル 2 件" in joined


# ---------------------------------------------------------------- state
def test_transitions_refuse_repeating_production():
    job = {"id": "j", "status": "approved"}
    state.transition(job, "producing", NOW)
    with pytest.raises(state.StateError):
        state.transition(job, "producing", NOW)
    assert job["history"][-1][1] == "producing"


def test_store_prunes_and_stays_under_variable_limit(tmp_path):
    store = make_store(tmp_path)
    data = state.empty()
    data["seen"] = {"old": NOW - 400 * 86400, "new": NOW}
    for i in range(400):
        data["jobs"][f"j{i}"] = {
            "id": f"j{i}",
            "status": "published",
            "updated_at": NOW - i,
            "title": "x" * 200,
        }
    data["jobs"]["live"] = {
        "id": "live",
        "status": "producing",
        "updated_at": NOW - 90 * 86400,
    }
    store.save(data)
    saved = store.load()
    assert "old" not in saved["seen"] and "new" in saved["seen"]
    assert "live" in saved["jobs"]  # unfinished jobs are never dropped
    assert len((tmp_path / "state.json").read_bytes()) <= state.MAX_BYTES


def test_variable_backend_creates_then_updates():
    fake = FakeHTTP()
    fake.on(
        "GET",
        "https://api.github.com/repos/o/r/actions/variables/AUTOPILOT_STATE",
        Reply(404, {}),
    )
    fake.on(
        "PATCH",
        "https://api.github.com/repos/o/r/actions/variables/AUTOPILOT_STATE",
        Reply(404, {}),
    )
    fake.on(
        "POST", "https://api.github.com/repos/o/r/actions/variables", Reply(201, {})
    )
    backend = state.VariableBackend("o/r", "tkn", requester=fake)
    assert backend.read() is None
    backend.write('{"version":1}')
    assert (
        fake.called("POST", "https://api.github.com/repos/o/r/actions/variables")[0][2][
            "json_body"
        ]["name"]
        == "AUTOPILOT_STATE"
    )


# ---------------------------------------------------------------- sources
FEED = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:yt="http://www.youtube.com/xml/schemas/2015">
<entry><yt:videoId>LIVE1</yt:videoId><title>live now</title><published>2027-01-15T00:00:00+00:00</published></entry>
<entry><yt:videoId>DONE1</yt:videoId><title>finished stream</title><published>2027-01-14T00:00:00+00:00</published></entry>
<entry><yt:videoId>SHORT</yt:videoId><title>short</title><published>2027-01-13T00:00:00+00:00</published></entry>
</feed>"""


def test_youtube_skips_live_and_reports_duration():
    fake = FakeHTTP()
    fake.on("GET", "https://www.youtube.com/feeds/", Reply(200, FEED.encode()))
    fake.on(
        "GET",
        "https://www.googleapis.com/youtube/v3/videos",
        Reply(
            200,
            {
                "items": [
                    {
                        "id": "LIVE1",
                        "snippet": {"liveBroadcastContent": "live"},
                        "contentDetails": {},
                    },
                    {
                        "id": "DONE1",
                        "snippet": {"liveBroadcastContent": "none"},
                        "contentDetails": {"duration": "PT2H3M"},
                        "liveStreamingDetails": {
                            "actualEndTime": "2027-01-14T03:00:00Z"
                        },
                    },
                    {
                        "id": "SHORT",
                        "snippet": {"liveBroadcastContent": "none"},
                        "contentDetails": {"duration": "PT45S"},
                    },
                ]
            },
        ),
    )
    channel = cfgmod.parse(CONFIG).channels[0]
    vods = sources.youtube(channel, request=fake, api_auth={"key": "k"})
    assert [v.video_id for v in vods] == ["DONE1", "SHORT"]
    assert vods[0].duration_seconds == 7380 and vods[1].duration_seconds == 45


def test_youtube_without_api_waits_for_settle_time():
    fake = FakeHTTP().on(
        "GET", "https://www.youtube.com/feeds/", Reply(200, FEED.encode())
    )
    channel = cfgmod.parse(CONFIG).channels[0]
    published = sources._timestamp("2027-01-15T00:00:00+00:00")
    vods = sources.youtube(channel, request=fake, now=published + 3600, settle_hours=8)
    assert "LIVE1" not in [v.video_id for v in vods]


def test_twitch_skips_archive_of_running_stream():
    fake = FakeHTTP()
    fake.on(
        "GET", "https://api.twitch.tv/helix/users", Reply(200, {"data": [{"id": "42"}]})
    )
    fake.on(
        "GET",
        "https://api.twitch.tv/helix/streams",
        Reply(200, {"data": [{"id": "S2", "type": "live"}]}),
    )
    fake.on(
        "GET",
        "https://api.twitch.tv/helix/videos",
        Reply(
            200,
            {
                "data": [
                    {
                        "id": "2",
                        "stream_id": "S2",
                        "title": "running",
                        "created_at": "2027-01-15T00:00:00Z",
                        "duration": "1h",
                    },
                    {
                        "id": "1",
                        "stream_id": "S1",
                        "title": "done",
                        "created_at": "2027-01-14T00:00:00Z",
                        "duration": "3h2m1s",
                        "url": "https://www.twitch.tv/videos/1",
                    },
                ]
            },
        ),
    )
    channel = cfgmod.parse(CONFIG).channels[1]
    vods = sources.twitch(channel, client_id="c", token="t", request=fake)
    assert [v.video_id for v in vods] == ["1"]
    assert vods[0].duration_seconds == 10921


def test_kick_parses_finished_videos():
    fake = FakeHTTP().on(
        "GET",
        "https://kick.com/api/v2/channels/someone/videos",
        Reply(
            200,
            [
                {"is_live": True, "video": {"uuid": "u-live"}},
                {
                    "is_live": False,
                    "session_title": "VOD",
                    "duration": 7_200_000,
                    "start_time": "2027-01-14 10:00:00",
                    "video": {"uuid": "u-1"},
                },
            ],
        ),
    )
    channel = cfgmod.Channel("kick", "someone", "Someone")
    vods = sources.kick(channel, request=fake)
    assert [(v.video_id, v.duration_seconds) for v in vods] == [("u-1", 7200.0)]
    assert vods[0].url == "https://kick.com/someone/videos/u-1"


# ---------------------------------------------------------------- x / youtube / tiktok
def test_oauth_signature_matches_published_example():
    keys = {
        "api_key": "xvz1evFS4wEEPTGEFPHBog",
        "api_secret": "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "access_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "access_secret": "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    }
    header = x.oauth_header(
        "POST",
        "https://api.twitter.com/1.1/statuses/update.json",
        keys,
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp=1318622958,
        params={
            "include_entities": "true",
            "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
        },
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header


def test_x_text_fits_and_keeps_url():
    text = x.fit("あ" * 300, "https://youtu.be/abc")
    assert text.endswith("https://youtu.be/abc") and x.weight(text) <= 280


def test_resumable_upload_resumes_after_dropped_chunk(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 600)
    fake = FakeHTTP()
    fake.on(
        "POST",
        youtube.TOKEN_URL,
        Reply(200, {"access_token": "AT", "expires_in": 3600}),
    )
    fake.on(
        "POST",
        youtube.UPLOAD_URL,
        Reply(200, {}, {"Location": "https://upload.example/session"}),
    )
    fake.on(
        "PUT",
        "https://upload.example/session",
        Reply(308, {}, {"Range": "bytes=0-255"}),
        http.HttpError(503, "https://upload.example/session"),
        Reply(308, {}, {"Range": "bytes=0-511"}),  # status query after the failure
        Reply(201, {"id": "VID123"}),
    )
    creds = youtube.Credentials("id", "secret", "refresh", request=fake)
    video_id = youtube.upload(
        creds,
        video,
        title="<t>",
        description="d",
        request=fake,
        chunk=256,
        sleep=lambda s: None,
    )
    assert video_id == "VID123"
    puts = fake.called("PUT", "https://upload.example/session")
    assert puts[-1][2]["headers"]["Content-Range"] == "bytes 512-599/600"
    body = fake.called("POST", youtube.UPLOAD_URL)[0][2]["json_body"]
    assert (
        body["snippet"]["title"] == "＜t＞"
        and body["status"]["privacyStatus"] == "private"
    )


def test_tiktok_chunk_plan():
    assert tiktok.plan_chunks(10) == (10, 1)
    chunk, count = tiktok.plan_chunks(100 * 1024 * 1024)
    assert chunk == tiktok.CHUNK and count == 10


def test_youtube_tags_respect_total_length():
    tags = youtube.clean_tags(["a" * 100] * 10)
    assert sum(len(t) for t in tags) + len(tags) - 1 <= 500


# ---------------------------------------------------------------- jobs
class FakeFinder:
    def __init__(self):
        self.vods = {}

    def find(self, channel):
        if channel.key == "twitch:bob_live" and self.vods.get("fail"):
            raise RuntimeError("down")
        return self.vods.get(channel.key, [])


class FakeNotifier:
    def __init__(self):
        self.sent, self.resolved = [], []

    def send(self, text, job_id=None, decisions=(), mention=False):
        self.sent.append((text, job_id, tuple(decisions)))
        return {"channel": "c", "message": str(len(self.sent))} if decisions else None

    def resolve(self, message, text):
        self.resolved.append((message, text))


class FakeProcess:
    def __init__(self, command, cwd=None, make_clips=2, code=0):
        self.command = command
        out = Path(command[command.index("--output") + 1])
        (out / "production-state.json").write_text('{"status":"observing"}')
        for i in range(1, make_clips + 1):
            folder = out / "clips" / f"clip-{i:03d}"
            folder.mkdir(parents=True)
            (folder / "video.mp4").write_bytes(b"v")
            (folder / "render.json").write_text(
                json.dumps(
                    {"selection": {"title": f"話題{i}", "reason": "盛り上がった"}}
                )
            )
        self.code = code

    def wait(self, timeout=None):
        return self.code


def vod(key, video_id, published, title="配信"):
    platform = key.split(":")[0]
    return sources.Vod(
        platform, key, video_id, f"https://example/{video_id}", title, published, 7200
    )


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    clock = {"now": NOW}
    store = make_store(tmp_path, clock=lambda: clock["now"])
    finder, notifier, dispatched, commands = FakeFinder(), FakeNotifier(), [], []
    uploads = []

    def fake_upload(creds, path, **kwargs):
        uploads.append(kwargs)
        return f"YT{len(uploads)}"

    monkeypatch.setattr(youtube, "upload", fake_upload)
    monkeypatch.setattr(youtube, "set_privacy", lambda *a, **k: None)

    def runner(command, cwd=None):
        commands.append(command)
        return FakeProcess(command)

    p = jobs.Autopilot(
        cfgmod.parse(CONFIG),
        store,
        env={},
        request=FakeHTTP(),
        notifier=notifier,
        finder=finder,
        dispatch=lambda jid, action="produce": dispatched.append(jid),
        clock=lambda: clock["now"],
        log=lambda *a: None,
        runner=runner,
    )
    p._youtube = object()  # credentials are faked through youtube.upload
    p.test = {
        "clock": clock,
        "finder": finder,
        "notifier": notifier,
        "dispatched": dispatched,
        "commands": commands,
        "uploads": uploads,
        "store": store,
        "tmp": tmp_path,
    }
    return p


def test_first_watch_records_backlog_then_asks_for_new_vods(pilot):
    t = pilot.test
    alice = "youtube:ucabcdefghijklmnopqrstuv"
    t["finder"].vods[alice] = [vod(alice, "OLD", NOW - 3600)]
    assert pilot.watch() == []
    assert t["store"].load()["jobs"] == {}  # backlog only
    t["finder"].vods[alice].append(vod(alice, "NEW", NOW - 600))
    pilot.watch()
    pilot.watch()  # a second look never duplicates
    jobs_now = t["store"].load()["jobs"]
    assert len(jobs_now) == 1
    job = next(iter(jobs_now.values()))
    assert job["status"] == "awaiting_approval"
    assert t["notifier"].sent[-1][2] == ("approve", "reject")
    assert job["message"]["message"]


def test_auto_channel_starts_production_and_failures_stay_isolated(pilot):
    t = pilot.test
    bob = "twitch:bob_live"
    t["finder"].vods[bob] = []
    pilot.watch()
    t["finder"].vods[bob] = [vod(bob, "V1", NOW - 60)]
    started = pilot.watch()
    assert started == t["dispatched"] and len(started) == 1
    assert t["store"].load()["jobs"][started[0]]["status"] == "approved"
    t["finder"].vods["fail"] = True
    pilot.watch()  # Bob failing does not raise


def test_full_approval_flow_to_x_post(pilot, monkeypatch):
    t = pilot.test
    alice = "youtube:ucabcdefghijklmnopqrstuv"
    pilot.watch()
    t["finder"].vods[alice] = [vod(alice, "NEW", NOW - 600, "雑談配信")]
    pilot.watch()
    jid = next(iter(t["store"].load()["jobs"]))
    pilot.decide(jid, "approve")
    assert t["dispatched"] == [jid]
    assert pilot.produce(jid, t["tmp"] / "work") is True
    job = t["store"].load()["jobs"][jid]
    assert job["status"] == "awaiting_publish"
    assert all(
        u["privacy"] == "private" for u in t["uploads"]
    )  # approve mode uploads privately
    assert job["production_state"] == '{"status":"observing"}'
    with pytest.raises(state.StateError):
        pilot.produce(jid)  # never a second production
    pilot.decide(jid, "publish")
    data = t["store"].load()
    assert data["jobs"][jid]["status"] == "published"
    assert set(data["videos"]) == {"YT1", "YT2"}
    # X waits for the scheduled time and a public video.
    posted = []
    monkeypatch.setattr(x, "keys_from_env", lambda env: {"k": 1})
    monkeypatch.setattr(
        x, "post", lambda text, keys, request: posted.append(text) or "P1"
    )
    monkeypatch.setattr(
        youtube,
        "videos",
        lambda creds, ids, request: {
            i: {"status": {"privacyStatus": "public"}} for i in ids
        },
    )
    assert pilot.post_due() == 0
    t["clock"]["now"] = NOW + 3 * 86400
    assert pilot.post_due() == 2
    assert all("https://youtu.be/YT" in p for p in posted)
    assert pilot.post_due() == 0  # posted once


def test_failed_production_retries_by_reconnecting(pilot):
    t = pilot.test
    bob = "twitch:bob_live"
    pilot.watch()
    t["finder"].vods[bob] = [vod(bob, "V1", NOW - 60)]
    jid = pilot.watch()[0]
    pilot.runner = lambda command, cwd=None: t["commands"].append(
        command
    ) or FakeProcess(command, make_clips=0, code=1)
    assert pilot.produce(jid, t["tmp"] / "w1") is False
    assert t["store"].load()["jobs"][jid]["status"] == "failed"
    pilot.runner = lambda command, cwd=None: t["commands"].append(
        command
    ) or FakeProcess(command)
    pilot.decide(jid, "retry")
    assert pilot.produce(jid, t["tmp"] / "w2") is True
    assert (
        "--resume" in t["commands"][-1]
        and "https://example/V1" not in t["commands"][-1]
    )
    job = t["store"].load()["jobs"][jid]
    assert job["status"] == "published"  # Bob publishes automatically
    scheduled = [u["publish_at"] for u in t["uploads"]]
    assert all(scheduled) and len(set(scheduled)) == 2


def test_unanswered_questions_expire(pilot):
    t = pilot.test
    alice = "youtube:ucabcdefghijklmnopqrstuv"
    pilot.watch()
    t["finder"].vods[alice] = [vod(alice, "NEW", NOW - 600)]
    pilot.watch()
    t["clock"]["now"] = NOW + 8 * 86400
    pilot.watch()
    assert next(iter(t["store"].load()["jobs"].values()))["status"] == "expired"
    assert "期限切れ" in t["notifier"].resolved[-1][1]


def test_next_slots_skip_taken_and_past_times():
    cfg = cfgmod.parse(CONFIG)
    base = 1_800_000_000.0
    slots = jobs.next_slots(3, base, cfg, [])
    assert slots == sorted(slots) and all(s >= base + 1800 for s in slots)
    hours = {time.gmtime(s + 9 * 3600).tm_hour for s in slots}
    assert hours <= {12, 18}
    assert jobs.next_slots(1, base, cfg, slots[:1])[0] == slots[1]


def test_feedback_names_strong_and_weak_topics():
    channels = {c.key: c for c in cfgmod.parse(CONFIG).channels}
    videos = {
        str(i): {
            "channel": "twitch:bob_live",
            "title": f"T{i}",
            "m": {"views": 100 + i, "averageViewPercentage": 10 * i},
        }
        for i in range(5)
    }
    note = jobs.feedback(videos, channels)["twitch:bob_live"]
    assert note.index("T4") < note.index("見られにくかった") < note.index("T0")


def test_metadata_template_and_gemini_fallback():
    cfg = cfgmod.parse(CONFIG)
    channel = cfg.channels[0]
    job = {"title": "雑談配信", "url": "https://youtu.be/src"}
    meta = metadata.template(
        {"title": "猫の話", "reason": "盛り上がった"}, job, channel, cfg
    )
    assert meta["title"] == "猫の話" and "https://youtu.be/src" in meta["description"]
    broken = FakeHTTP().on(
        "POST", "https://aiplatform", http.HttpError(500, "https://aiplatform")
    )
    same = metadata.build(
        {"title": "猫の話"},
        job,
        channel,
        cfg,
        env={"GOOGLE_OAUTH_ACCESS_TOKEN": "t"},
        request=broken,
        log=lambda *a: None,
    )
    assert same["title"] == "猫の話"
    assert metadata.x_text(
        meta,
        "https://youtu.be/v",
        cfgmod.parse(CONFIG + 'x_template: "{title} {unknown}"'),
    ).endswith("https://youtu.be/v")


def test_notifier_buttons_and_webhook_fallback():
    rows = notify.buttons("abc", ("approve", "reject"))
    assert [b["custom_id"] for b in rows[0]["components"]] == [
        "ap:approve:abc",
        "ap:reject:abc",
    ]
    fake = FakeHTTP().on("POST", "https://discord.test/hook", Reply(204, b""))
    n = notify.Notifier(
        {"DISCORD_WEBHOOK_URL": "https://discord.test/hook"}, fake, echo=lambda *a: None
    )
    assert n.send("hi", "abc", ("approve",)) is None
    assert "action=decide" in fake.calls[0][2]["json_body"]["content"]


def test_redact_hides_tokens():
    assert "abc123" not in http.redact('{"access_token": "abc123", "x": 1}')


def test_produce_checkpoint_survives_slow_process(pilot, monkeypatch):
    """The production state is saved while ClipForge is still running."""
    t = pilot.test
    bob = "twitch:bob_live"
    pilot.watch()
    t["finder"].vods[bob] = [vod(bob, "V9", NOW - 60)]
    jid = pilot.watch()[0]

    class Slow(FakeProcess):
        def __init__(self, command, cwd=None):
            super().__init__(command)
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("x", timeout)
            return 0

    pilot.runner = lambda command, cwd=None: Slow(command)
    assert pilot.produce(jid, t["tmp"] / "slow")


def test_publish_runs_as_a_separate_step(pilot):
    t = pilot.test
    bob = "twitch:bob_live"
    pilot.watch()
    t["finder"].vods[bob] = [vod(bob, "V5", NOW - 60)]
    jid = pilot.watch()[0]
    work = t["tmp"] / "split"
    assert pilot.produce(jid, work, publish=False)
    assert t["store"].load()["jobs"][jid]["status"] == "produced"
    assert t["uploads"] == []
    assert pilot.publish_from(jid, work) == "published"
    with pytest.raises(ValueError):
        pilot.publish_from(jid, work)


def test_audience_note_is_advisory_and_bounded():
    from clipforge.audio_understanding import audience_note

    assert audience_note({}) == ""
    note = audience_note(
        {"CLIPFORGE_AUDIENCE_FEEDBACK": "よく見られた話題\n" + "あ" * 2000}
    )
    assert "音声の内容を優先" in note and "\n" not in note and len(note) < 1200


def test_example_config_loads_and_bare_off_means_off():
    example = (
        Path(__file__).resolve().parents[1] / "autopilot.example.yaml"
    ).read_text(encoding="utf-8")
    cfg = cfgmod.parse(example)
    assert [c.mode for c in cfg.channels] == ["approve", "approve", "off"]
