"""Synthetic chat only; no network access (the session is a fake)."""

import datetime as dt
import re

import pytest

from clipforge import kick_chat
from clipforge.storage import PipelineError

CHANNEL = "testchan"
CHANNEL_ID = 77
VOD = "vod-uuid"
ORIGIN = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
BASE = f"{kick_chat.KICK_V2}/{CHANNEL}"
VIDEOS = f"{BASE}/videos"
MESSAGES = f"{kick_chat.KICK_V2}/{CHANNEL_ID}/messages"
EVENT = re.compile(
    r"Dialogue: 0,([\d:.]+),([\d:.]+),(Chat|Head),,0,0,0,,"
    r"\{\\an\d\\pos\(([\d.]+),(\d+)\)\}(.*)"
)


def iso(seconds):
    return (ORIGIN + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def row(mid, seconds, *, content="hi", name="alice", color="#112233"):
    return {
        "id": mid,
        "created_at": iso(seconds),
        "content": content,
        "sender": {"username": name, "identity": {"color": color}},
    }


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Serves channel, VOD list and chat replay from a list; plan queues failures per URL."""

    def __init__(self, messages, *, plan=None):
        self.messages = messages
        self.plan = plan or {}
        self.calls = []

    def get(self, url, *, params=None, timeout=None):
        self.calls.append((url, params))
        queued = self.plan.get(url)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return Response(item)
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
            rows = [
                m
                for m in self.messages
                if cursor <= kick_chat._parse_time(m["created_at"]) < cursor + window
            ]
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


def test_transient_failures_are_retried(no_sleep):
    session = FakeSession([row(1, 30)], plan={BASE: [429, 503], MESSAGES: [500]})

    assert len(fetch(session)) == 1
    assert no_sleep == [1.0, 2.0, 1.0]


def test_transport_errors_fail_without_leaking_url(no_sleep):
    session = FakeSession(
        [], plan={MESSAGES: [ConnectionError(f"reset while reading {MESSAGES}")] * 4}
    )

    with pytest.raises(PipelineError) as info:
        fetch(session)

    assert "kick.com" not in str(info.value)
    assert "reset" not in str(info.value)
    assert sum(1 for url, _ in session.calls if url == MESSAGES) == 4


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


def test_page_boundary_replays_the_same_second_and_deduplicates_ids(no_sleep):
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
