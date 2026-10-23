from types import SimpleNamespace

from clipforge import chat_activity, kick_chat
from clipforge.storage import PipelineError

URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"


def sample():
    analysis = {
        "duration": 14400,
        "topics": [
            {"id": "a", "start": 300, "end": 310},
            {"id": "b", "start": 305, "end": 315},
            {"id": "c", "start": 900, "end": 910},
        ],
    }
    previews = {
        "previews": [
            {"topic_id": "a", "file": "a.mp4"},
            {"topic_id": "b", "file": "b.mp4"},
        ]
    }
    return analysis, previews


def test_only_shortlisted_intervals_are_fetched_and_overlaps_are_merged(monkeypatch):
    calls = []

    def fetch(channel, uuid, start, end, *, lead):
        calls.append((start, end, lead))
        return [{"t": 305, "name": "PRIVATE_NAME", "text": "PRIVATE_CHAT"}]

    monkeypatch.setattr(kick_chat, "fetch_messages", fetch)
    series, info = chat_activity.candidate_series(SimpleNamespace(url=URL), *sample())
    assert calls == [(300, 315, 0)]
    assert dict(series)[305] == 1
    assert len(series) == 15
    assert "PRIVATE" not in str((series, info))
    assert info["data"] == "available"


def test_failed_chat_is_unavailable_and_never_zero_activity(monkeypatch):
    def fail(*args, **kwargs):
        raise PipelineError("private transport detail")

    monkeypatch.setattr(kick_chat, "fetch_messages", fail)
    series, info = chat_activity.candidate_series(SimpleNamespace(url=URL), *sample())
    assert series is None
    assert info["reason"] == "fetch_failed"
    assert "private transport" not in str(info)


def test_other_sources_do_not_make_a_chat_request(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no network for other sources")

    monkeypatch.setattr(kick_chat, "fetch_messages", fail)
    series, info = chat_activity.candidate_series(
        SimpleNamespace(url="https://example.test/video.mp4"), *sample()
    )
    assert series is None
    assert info["reason"] == "unsupported_source"
