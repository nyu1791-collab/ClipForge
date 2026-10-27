"""Synthetic chat only; no network access (the session is a fake)."""

import datetime as dt
import random
import re
import sys
import types

import pytest

from clipforge import kick_chat
from clipforge.storage import PipelineError

CHANNEL = "testchan"
CHANNEL_ID = 77
VOD = "vod-uuid"
API_LIMIT = 25  # messages the replay API returns per request, as observed
ORIGIN = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
BASE = f"{kick_chat.KICK_V2}/{CHANNEL}"
VIDEOS = f"{BASE}/videos"
MESSAGES = f"{kick_chat.KICK_V2}/{CHANNEL_ID}/messages"
WRAP = (480 - 22) / 34  # default 1080p panel: wrap width in full-width characters
EVENT = re.compile(
    r"Dialogue: 0,([\d:.]+),([\d:.]+),(Chat|Head),,0,0,0,,"
    r"\{\\an\d\\pos\(([\d.]+),(\d+)\)\}(.*)"
)


def iso(seconds):
    moment = ORIGIN + dt.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def row(mid, seconds, *, content="hi", name="alice", color="#112233"):
    return {
        "id": mid,
        "created_at": iso(seconds),
        "content": content,
        "sender": {"username": name, "identity": {"color": color}},
    }


class Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def fresh_cache():
    kick_chat._reset_cache()
    yield
    kick_chat._reset_cache()


class FakeSession:
    """Serves channel, VOD list and chat replay from a list; plan queues failures per URL.

    Chat works like the real replay: the 5 seconds from start_time, in time order,
    cut off at API_LIMIT messages.
    """

    def __init__(self, messages, *, plan=None, order="ascending"):
        self.messages = messages
        self.plan = plan or {}
        self.order = order  # "ascending", "descending" or "shuffled" within a response
        self.calls = []

    def get(self, url, *, params=None, timeout=None):
        self.calls.append((url, params))
        queued = self.plan.get(url)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, Response) else Response(item)
        if url == BASE:
            return Response(200, {"id": CHANNEL_ID, "slug": CHANNEL})
        if url == VIDEOS:
            return Response(
                200,
                [
                    {"video": {"uuid": "other"}, "start_time": "2026-01-01 00:00:00"},
                    {"video": {"uuid": VOD}, "start_time": "2026-10-20 12:00:00"},
                ],
            )
        if url == MESSAGES:
            cursor = dt.datetime.strptime(
                params["start_time"], kick_chat.TIME_FORMAT
            ).replace(tzinfo=dt.timezone.utc)
            window = dt.timedelta(seconds=5)
            when = {
                id(m): kick_chat._parse_time(m["created_at"]) for m in self.messages
            }
            rows = sorted(
                (m for m in self.messages if cursor <= when[id(m)] < cursor + window),
                key=lambda m: when[id(m)],
            )[:API_LIMIT]
            if self.order == "descending":
                rows.reverse()
            elif self.order == "shuffled":
                random.Random(len(self.calls)).shuffle(rows)
            return Response(200, {"data": {"messages": rows}})
        return Response(404)


def fetch(session, start=20, end=60, lead=10):
    return kick_chat.fetch_messages(
        CHANNEL, VOD, start, end, lead=lead, session=session
    )


def chat_rows(ass):
    rows = []
    for line in ass.splitlines():
        match = EVENT.match(line)
        if match and match.group(3) == "Chat":
            rows.append(
                {
                    "start": _seconds(match.group(1)),
                    "end": _seconds(match.group(2)),
                    "x": float(match.group(4)),
                    "y": int(match.group(5)),
                    "text": re.sub(r"\{[^}]*\}", "", match.group(6)),
                }
            )
    return rows


def _seconds(stamp):
    h, m, rest = stamp.split(":")
    s, c = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(c) / 100


@pytest.fixture
def no_sleep(monkeypatch):
    pauses = []
    monkeypatch.setattr(kick_chat, "_pause", pauses.append)
    return pauses


def test_pages_collect_dedupe_and_filter_range(no_sleep):
    msgs = [row(i, s) for i, s in enumerate([5, 10, 17, 23, 40, 58, 61])]
    msgs.append(row(3, 23))  # same id returned twice
    session = FakeSession(msgs)

    got = fetch(session)

    assert [m["t"] for m in got] == [10.0, 17.0, 23.0, 40.0, 58.0]
    chat_calls = [params for url, params in session.calls if url == MESSAGES]
    assert chat_calls[0]["start_time"] == iso(10)
    assert len(chat_calls) > 3


def test_time_is_seconds_from_utc_vod_start(no_sleep):
    got = fetch(FakeSession([row(1, 90, name="bob")]), start=0, end=100, lead=90)

    assert got == [{"t": 90.0, "name": "bob", "color": "#112233", "text": "hi"}]


def test_emotes_are_named_and_controls_removed(no_sleep):
    msgs = [row(1, 30, content="hi [emote:12345:Kappa]\x01 yo\n!")]

    assert fetch(FakeSession(msgs))[0]["text"] == "hi :Kappa: yo !"


def test_repeated_emotes_are_shown_once_with_a_count(no_sleep):
    run = " ".join(["[emote:7:gen626mitogen]"] * 8)
    msgs = [
        row(
            1,
            30,
            content=f"{run}[emote:7:gen626mitogen] [emote:9:wave] ok [emote:9:wave]",
        )
    ]

    assert fetch(FakeSession(msgs))[0]["text"] == ":gen626mitogen: ×9 :wave: ok :wave:"


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        ("#a1b2c3", "#A1B2C3"),
        ("red", "#FFFFFF"),
        ("#abc", "#FFFFFF"),
        (None, "#FFFFFF"),
    ],
)
def test_color_is_validated(color, expected, no_sleep):
    got = fetch(FakeSession([row(1, 30, color=color)]))

    assert got[0]["color"] == expected


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_failures_are_not_retried(no_sleep, status):
    session = FakeSession([row(1, 30)], plan={BASE: [status]})

    with pytest.raises(PipelineError, match=f"HTTP {status}"):
        fetch(session)
    assert len(session.calls) == 1 and no_sleep == []


def test_transport_errors_fail_without_leaking_url(no_sleep):
    session = FakeSession(
        [], plan={MESSAGES: [ConnectionError(f"reset while reading {MESSAGES}")] * 4}
    )

    with pytest.raises(PipelineError) as info:
        fetch(session)

    assert "kick.com" not in str(info.value)
    assert "reset" not in str(info.value)
    assert sum(1 for url, _ in session.calls if url == MESSAGES) == 1
    assert no_sleep == []


def test_final_http_failure_has_status_only(no_sleep):
    session = FakeSession([row(1, 30)], plan={MESSAGES: [429] * 10})

    with pytest.raises(PipelineError) as info:
        fetch(session)

    assert "HTTP 429" in str(info.value)
    assert "kick.com" not in str(info.value)
    assert "start_time" not in str(info.value)


def test_client_errors_are_not_retried(no_sleep):
    session = FakeSession([], plan={BASE: [404]})

    with pytest.raises(PipelineError, match="HTTP 404"):
        fetch(session)
    assert no_sleep == []
    assert sum(1 for url, _ in session.calls if url == BASE) == 1


def test_unknown_vod_is_an_error(no_sleep):
    with pytest.raises(PipelineError):
        kick_chat.fetch_messages(
            CHANNEL, "missing", 20, 60, session=FakeSession([row(1, 30)])
        )


def test_empty_range_is_rejected():
    with pytest.raises(ValueError):
        kick_chat.fetch_messages(CHANNEL, VOD, 30, 30, session=FakeSession([]))


def test_newest_message_sits_at_bottom_and_older_stack_up():
    msgs = [
        {"t": 10.0, "name": "a", "color": "#112233", "text": "one"},
        {"t": 11.0, "name": "a", "color": "#112233", "text": "two"},
        {"t": 12.0, "name": "a", "color": "#112233", "text": "three"},
    ]
    ass = kick_chat.panel_ass(msgs, 0.0, 60.0)

    final = {
        r["text"].split("  ")[1]: r["y"] for r in chat_rows(ass) if r["end"] == 60.0
    }

    assert final == {"three": 1050, "two": 1050 - 58, "one": 1050 - 116}
    assert "&H332211&" in ass


def test_stack_stays_inside_the_panel():
    msgs = [
        {"t": float(i), "name": f"u{i:02d}", "color": "#FFFFFF", "text": f"m{i:02d}"}
        for i in range(40)
    ]
    rows = chat_rows(kick_chat.panel_ass(msgs, 0.0, 60.0))

    ys = [r["y"] for r in rows]
    assert max(ys) == 1050
    assert min(ys) >= 90 + 46
    assert len({r["y"] for r in rows if r["end"] == 60.0}) == 16


def test_burst_drops_oldest_and_keeps_each_message_visible():
    msgs = [
        {
            "t": 10.0 + i * 0.02,
            "name": f"u{i:02d}",
            "color": "#FFFFFF",
            "text": f"m{i:02d}",
        }
        for i in range(40)
    ]
    msgs.append({"t": 12.0, "name": "zz", "color": "#FFFFFF", "text": "late"})
    rows = chat_rows(kick_chat.panel_ass(msgs, 0.0, 60.0))

    shown = {}
    for r in rows:
        name = r["text"].split("  ")[0]
        shown.setdefault(name, []).append(r)
    assert not any(f"u{i:02d}" in shown for i in range(24))
    assert all(f"u{i:02d}" in shown for i in range(24, 40))
    for name, parts in shown.items():
        start = min(p["start"] for p in parts)
        end = max(p["end"] for p in parts)
        if name == "zz":
            continue
        if end < 60.0:
            assert end - start >= 1.5 - 0.011, name


def test_messages_before_the_clip_show_from_time_zero():
    msgs = [
        {"t": 70.0, "name": "early", "color": "#FFFFFF", "text": "before"},
        {"t": 100.0, "name": "in", "color": "#FFFFFF", "text": "during"},
        {"t": 130.0, "name": "late", "color": "#FFFFFF", "text": "after"},
    ]
    rows = chat_rows(kick_chat.panel_ass(msgs, 90.0, 120.0))

    first_shown = {}
    for r in rows:
        first_shown.setdefault(r["text"].split("  ")[0], r["start"])
    assert first_shown["early"] == 0.0
    assert first_shown["in"] == 10.0
    assert "late" not in first_shown


def test_anonymize_is_stable_and_hides_names():
    msgs = [
        {"t": 1.0, "name": "alice", "color": "#FFFFFF", "text": "x"},
        {"t": 2.0, "name": "alice", "color": "#FFFFFF", "text": "y"},
        {"t": 3.0, "name": "bob", "color": "#FFFFFF", "text": "z"},
    ]
    ass = kick_chat.panel_ass(msgs, 0.0, 10.0, anonymize=True)

    assert "alice" not in ass and "bob" not in ass
    names = {r["text"].split("  ")[0] for r in chat_rows(ass)}
    assert len(names) == 2
    assert all(n.startswith("視聴者") and len(n) == 6 for n in names)
    assert kick_chat.panel_ass(msgs, 0.0, 10.0, anonymize=True) == ass


def test_ass_special_characters_are_replaced():
    msgs = [{"t": 1.0, "name": "a{b}", "color": "#FFFFFF", "text": "x{y}\\z"}]
    ass = kick_chat.panel_ass(msgs, 0.0, 10.0)

    assert [r["text"] for r in chat_rows(ass)] == ["a(b)  x(y)＼z"]
    assert "\\z" not in ass


def test_geometry_scales_with_frame_size():
    msgs = [{"t": 1.0, "name": "u", "color": "#FFFFFF", "text": "あ" * 40}]
    small = kick_chat.panel_ass(msgs, 0.0, 10.0)
    big = kick_chat.panel_ass(msgs, 0.0, 10.0, width=3840, height=2160, panel_x=2880)

    assert "Style: Chat,Noto Sans CJK JP,34," in small
    assert "Style: Chat,Noto Sans CJK JP,68," in big
    small_rows = chat_rows(small)
    big_rows = chat_rows(big)
    assert len(small_rows) == len(big_rows) == 4
    assert [r["y"] for r in small_rows] == [1050 - 46 * i for i in range(4)]
    assert [r["y"] for r in big_rows] == [2100 - 92 * i for i in range(4)]
    assert small_rows[1]["x"] == 1462 + 34
    assert big_rows[1]["x"] == 2924 + 68


def test_long_messages_are_capped_with_an_ellipsis():
    msgs = [{"t": 1.0, "name": "u", "color": "#FFFFFF", "text": "あ" * 300}]
    rows = chat_rows(kick_chat.panel_ass(msgs, 0.0, 10.0))

    assert len(rows) == 6
    assert any(r["text"].endswith("…") for r in rows)


def test_empty_clip_is_rejected():
    with pytest.raises(ValueError):
        kick_chat.panel_ass([], 10.0, 10.0)


def test_page_boundary_replays_the_same_second_and_deduplicates_ids(
    no_sleep, monkeypatch
):
    monkeypatch.setattr(kick_chat, "PAGE_LIMIT", 2)

    class BurstSession(FakeSession):
        def __init__(self):
            super().__init__([])
            self.pages = [
                [row("a", 11, content="first"), row("b", 12, content="second")],
                [row("b", 12, content="second"), row("c", 12, content="boundary")],
                [row("b", 12, content="second"), row("c", 12, content="boundary")],
            ]

        def get(self, url, *, params=None, timeout=None):
            if url == MESSAGES and self.pages:
                self.calls.append((url, params))
                return Response(200, {"data": {"messages": self.pages.pop(0)}})
            return super().get(url, params=params, timeout=timeout)

    session = BurstSession()
    result = fetch(session, start=10, end=16, lead=0)
    assert [m["text"] for m in result] == ["first", "second", "boundary"]
    cursors = [p["start_time"] for u, p in session.calls if u == MESSAGES]
    assert cursors[:3] == [iso(10), iso(12), iso(12)]
    assert len(cursors) < 10


def chat_calls(session):
    return [params for url, params in session.calls if url == MESSAGES]


def lookups(session):
    return sum(1 for url, _ in session.calls if url in (BASE, VIDEOS))


def test_dense_chat_is_collected_past_the_page_limit(no_sleep):
    # 20 messages a second over [10, 40): each page of 25 covers only about 1.25 seconds.
    msgs = [row(i, 10 + i * 0.05, content=f"m{i}") for i in range(600)]

    got = fetch(FakeSession(msgs), start=20, end=40, lead=10)

    assert {m["text"] for m in got} == {f"m{i}" for i in range(600)}
    assert len(got) == 600


@pytest.mark.parametrize("order", ["descending", "shuffled"])
def test_dense_chat_is_collected_whatever_the_page_order(order, no_sleep):
    # Same dense burst as above, but each response lists its messages out of time order.
    msgs = [row(i, 10 + i * 0.05, content=f"m{i}") for i in range(600)]

    got = fetch(FakeSession(msgs, order=order), start=20, end=40, lead=10)

    assert {m["text"] for m in got} == {f"m{i}" for i in range(600)}
    assert len(got) == 600


def test_full_pages_without_new_messages_still_move_on(no_sleep):
    # Every request answers with the same 25 messages whatever the cursor.
    class StuckSession(FakeSession):
        def get(self, url, *, params=None, timeout=None):
            if url != MESSAGES:
                return super().get(url, params=params, timeout=timeout)
            self.calls.append((url, params))
            return Response(
                200, {"data": {"messages": [row(i, 12) for i in range(25)]}}
            )

    session = StuckSession([])
    got = fetch(session, start=20, end=40, lead=10)

    assert len(got) == 25
    assert len(chat_calls(session)) <= 31  # one repeat, then one request per second


def test_burst_longer_than_a_page_does_not_stop_paging(no_sleep):
    msgs = (
        [row(0, 10.0, content="early")]
        + [row(i, 11.5, content=f"burst{i}") for i in range(1, 31)]
        + [row(31, 12.5, content="late")]
    )

    texts = [m["text"] for m in fetch(FakeSession(msgs), start=20, end=40, lead=10)]

    assert texts[0] == "early"
    assert "late" in texts
    assert len(texts) == len(set(texts))


def test_first_line_with_name_fits_the_wrap_width(no_sleep):
    msgs = [{"t": 1.0, "name": "a", "color": "#FFFFFF", "text": "あ" * 30}]
    ass = kick_chat.panel_ass(msgs, 0.0, 10.0)

    first = next(r["text"] for r in chat_rows(ass) if r["text"].startswith("a  "))
    width = sum(kick_chat._char_width(ch) for ch in first)

    assert width <= WRAP + 1e-9
    assert first == "a  " + "あ" * 11


@pytest.mark.parametrize(
    "header",
    [
        "7",
        "999",
        "Fri, 01 Jan 2100 00:00:00 GMT",
        "Wed, 21 Oct 2015 07:28:00 GMT",
        "soon",
    ],
)
def test_429_does_not_retry_even_with_retry_after(header, no_sleep):
    session = FakeSession(
        [row(1, 30)],
        plan={MESSAGES: [Response(429, headers={"Retry-After": header})]},
    )

    with pytest.raises(PipelineError, match="HTTP 429"):
        fetch(session)
    assert sum(1 for url, _ in session.calls if url == MESSAGES) == 1
    assert no_sleep == []


def test_channel_and_vod_lookups_are_cached_per_vod(no_sleep):
    msgs = [row(1, 30)]
    first = FakeSession(msgs)
    fetch(first)
    again = FakeSession(msgs)
    fetch(again)
    upper = FakeSession(msgs)
    kick_chat.fetch_messages(CHANNEL.upper(), VOD, 20, 60, lead=10, session=upper)

    assert lookups(first) == 2
    assert lookups(again) == 0
    assert lookups(upper) == 0

    kick_chat._reset_cache()
    fresh = FakeSession(msgs)
    fetch(fresh)
    assert lookups(fresh) == 2


def test_default_session_is_created_once_and_reused(monkeypatch, no_sleep):
    made = []

    class Session(FakeSession):
        def __init__(self, impersonate=None):
            super().__init__([row(1, 30)])
            self.impersonate = impersonate
            made.append(self)

    fake_module = types.SimpleNamespace(requests=types.SimpleNamespace(Session=Session))
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_module)

    first = kick_chat.fetch_messages(CHANNEL, VOD, 20, 60, lead=10)
    second = kick_chat.fetch_messages(CHANNEL, VOD, 20, 60, lead=10)

    assert first == second and len(first) == 1
    assert len(made) == 1
    assert made[0].impersonate == "chrome"
    assert lookups(made[0]) == 2

    kick_chat._reset_cache()
    kick_chat.fetch_messages(CHANNEL, VOD, 20, 60, lead=10)
    assert len(made) == 2
    assert lookups(made[1]) == 2
