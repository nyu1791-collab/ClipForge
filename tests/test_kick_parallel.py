import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import kick_media as km
from clipforge.storage import PipelineError

URL = "https://kick.com/example/videos/01a0fac4-e9a0-72b3-82b7-2fd2e83b16a5"
ROOT = "https://stream.kick.com/archive/"
MIB = 1024**2


@pytest.fixture
def make_vod(tmp_path, monkeypatch):
    values = []

    def make(count=12, **kwargs):
        state = SimpleNamespace(
            calls=[],
            sessions=[],
            rendered=[],
            finished=[],
            statuses={},
            download=lambda index: b"G" + bytes([index]),
        )
        lock = threading.Lock()
        playlist = (
            "#EXTM3U\n"
            + "".join(f"#EXTINF:1,\n{index}.ts\n" for index in range(count))
            + "#EXT-X-ENDLIST\n"
        )
        data = {
            URL: '<script type="application/ld+json">'
            + json.dumps(
                {"@type": "VideoObject", "url": URL, "contentUrl": ROOT + "master.m3u8"}
            )
            + "</script>",
            ROOT + "master.m3u8": (
                "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\nmedia.m3u8\n"
                "#EXT-X-STREAM-INF:RESOLUTION=1920x1080\nmedia.m3u8\n"
            ),
            ROOT + "media.m3u8": playlist,
        }

        class Session:
            def __init__(self, **options):
                self.options, self.owner, self.closed = options, None, 0
                with lock:
                    state.sessions.append(self)

            def get(self, url, **options):
                assert options == {
                    "allow_redirects": False,
                    "timeout": 30,
                    "stream": True,
                }
                if not url.endswith(".ts"):
                    if url not in data:
                        return SimpleNamespace(
                            status_code=404,
                            headers={},
                            iter_content=lambda: [],
                            close=lambda: None,
                        )
                    return SimpleNamespace(
                        status_code=200,
                        iter_content=lambda: [data[url].encode()],
                        close=lambda: None,
                    )
                index = int(url.rsplit("/", 1)[1][:-3])
                ident = threading.get_ident()
                with lock:
                    assert self is not state.sessions[0]
                    assert self.owner in (None, ident)
                    self.owner = ident
                    state.calls.append(index)

                def chunks():
                    body = state.download(index)
                    yield from body if isinstance(body, list) else [body]

                def close():
                    with lock:
                        state.finished.append(index)

                status = state.statuses.get(index, 200)
                if isinstance(status, list):
                    status = status.pop(0)
                return SimpleNamespace(
                    status_code=status,
                    iter_content=chunks,
                    close=close,
                )

            def close(self):
                self.closed += 1

        monkeypatch.setitem(
            sys.modules,
            "curl_cffi",
            SimpleNamespace(requests=SimpleNamespace(Session=Session)),
        )
        value = km.KickVOD(URL, tmp_path / f"cache-{len(values)}", **kwargs)
        values.append(value)

        def process(command, **options):
            if command[0] == "ffmpeg":
                state.rendered.append(
                    Path(command[command.index("-i") + 1]).read_bytes()
                )
                Path(command[-1]).write_bytes(b"synthetic rendered media")
                state.duration = len(state.rendered[-1]) / 2
                return SimpleNamespace(stdout=b"")
            if "-select_streams" in command:
                return SimpleNamespace(
                    stdout=json.dumps({"streams": [{"start_time": 100}]})
                )
            return SimpleNamespace(
                stdout=json.dumps({"format": {"duration": state.duration}})
            )

        monkeypatch.setattr(km, "process", process)
        return value, state

    yield make
    for value in values:
        value.close()


def test_six_workers_download_concurrently_and_concatenate_in_stream_order(
    make_vod, tmp_path
):
    value, state = make_vod()
    barrier = threading.Barrier(6)
    finished = [threading.Event() for _ in range(6)]
    completion_order = []

    def download(index):
        if index < 6:
            barrier.wait(timeout=5)
            if index < 5:
                assert finished[index + 1].wait(5)
            completion_order.append(index)
            finished[index].set()
        return b"G" + bytes([index])

    state.download = download
    result = value.retrieve(tmp_path / "video.mp4")
    assert completion_order == [5, 4, 3, 2, 1, 0]
    assert state.rendered == [b"".join(b"G" + bytes([i]) for i in range(12))]
    assert result["segment_count"] == 12
    assert sorted(state.calls) == list(range(12))
    assert len(state.sessions) == 7
    assert len({session.owner for session in state.sessions[1:]}) == 6
    assert all(
        session.options == state.sessions[0].options for session in state.sessions
    )
    assert state.sessions[0].options["impersonate"] == "chrome"
    assert state.sessions[0].options["headers"] == {"Referer": "https://kick.com/"}
    value.close()
    assert all(session.closed == 1 for session in state.sessions)


@pytest.mark.parametrize("operation", ["prefetch", "retrieve"])
def test_first_failure_is_raised_without_waiting_or_starting_more_segments(
    make_vod, tmp_path, operation
):
    value, state = make_vod(count=20)
    barrier = threading.Barrier(6)
    release = threading.Event()
    error = PipelineError("synthetic download failure")

    def download(index):
        barrier.wait(timeout=5)
        if index == 5:
            raise error
        assert release.wait(5)
        return b"Gx"

    state.download = download
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = (
            pool.submit(value.prefetch, 0, 20)
            if operation == "prefetch"
            else pool.submit(value.retrieve, tmp_path / "video.mp4")
        )
        try:
            with pytest.raises(PipelineError) as caught:
                future.result(timeout=5)
            assert caught.value is error
            assert sorted(state.calls) == list(range(6))
            assert state.rendered == []
        finally:
            release.set()
    value.close()
    assert sorted(state.calls) == list(range(6))
    assert all(session.closed == 1 for session in state.sessions)
    assert not (tmp_path / "video.mp4").exists()


def test_concurrent_prefetch_and_retrieve_share_each_inflight_url(
    make_vod, monkeypatch, tmp_path
):
    value, state = make_vod(count=6)
    started = threading.Event()
    barrier = threading.Barrier(6, action=started.set)
    release = threading.Event()
    shared = threading.Event()
    seen = {}
    original = value._segment_future

    def segment_future(url):
        future = original(url)
        with value._lock:
            previous = seen.setdefault(url, future)
            assert previous is future
            if len(seen) == 6 and all(
                sum(item == url for item in requests) >= 2 for url in seen
            ):
                shared.set()
        return future

    requests = []

    def track(url):
        with value._lock:
            requests.append(url)
        return segment_future(url)

    def download(index):
        barrier.wait(timeout=5)
        assert release.wait(5)
        return b"G" + bytes([index])

    state.download = download
    monkeypatch.setattr(value, "_segment_future", track)
    with ThreadPoolExecutor(max_workers=2) as pool:
        prefetch = pool.submit(value.prefetch, 0, 6)
        retrieve = pool.submit(value.retrieve, tmp_path / "video.mp4")
        try:
            assert shared.wait(5)
            assert started.wait(5)
            assert sorted(state.calls) == list(range(6))
        finally:
            release.set()
        prefetch.result(timeout=5)
        retrieve.result(timeout=5)
    assert sorted(state.calls) == list(range(6))
    assert value.used == 12
    assert len(state.rendered) == 1


def test_failure_cancels_queued_futures_before_they_start(make_vod, monkeypatch):
    value, state = make_vod(count=8, workers=2)
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    queued = threading.Event()
    error = PipelineError("synthetic queued download failure")
    original = value._segment_future

    def track(url):
        future = original(url)
        if url == ROOT + "3.ts":
            queued.set()
        return future

    def download(index):
        if index < 2:
            started[index].set()
            assert release[index].wait(5)
            return b"Gx"
        assert index == 2, "a cancelled segment must never start"
        raise error

    state.download = download
    monkeypatch.setattr(value, "_segment_future", track)
    with ThreadPoolExecutor(max_workers=3) as pool:
        blockers = [pool.submit(value.segment, ROOT + f"{i}.ts") for i in range(2)]
        try:
            assert all(event.wait(5) for event in started)
            prefetch = pool.submit(value.prefetch, 2, 8)
            assert queued.wait(5)
            release[0].set()
            with pytest.raises(PipelineError) as caught:
                prefetch.result(timeout=5)
            assert caught.value is error
        finally:
            for event in release:
                event.set()
        for future in blockers:
            future.result(timeout=5)
    value.close()
    assert sorted(state.calls) == [0, 1, 2]


def test_reservations_bound_parallel_downloads_and_settle_actual_bytes(make_vod):
    value, state = make_vod(count=3, max_bytes=128 * MIB)
    barrier = threading.Barrier(2)
    observed = []

    def download(index):
        with value._lock:
            observed.append((value.used, value._reserved))
            first_pair = len(observed) <= 2
        if first_pair:
            barrier.wait(timeout=5)
        return b"Gsynthetic"

    state.download = download
    value.prefetch(0, 3)
    assert len(observed) == 3
    assert all(0 < reserved <= value.max_bytes - used for used, reserved in observed)
    assert sorted(state.calls) == [0, 1, 2]
    assert value.used == 3 * len(b"Gsynthetic")
    assert value._reserved == 0


def test_exhausted_budget_never_starts_another_request(make_vod):
    value, state = make_vod(count=12, max_bytes=1024)
    state.download = lambda index: b"G" + b"x" * 1023
    with pytest.raises(PipelineError, match="segment byte budget exhausted"):
        value.prefetch(0, 12)
    value.close()
    assert len(state.calls) == 1
    assert value.used == value.max_bytes == 1024
    assert value._reserved == 0


def test_oversized_response_releases_its_reservation_without_retry(make_vod):
    value, state = make_vod(count=1, max_bytes=1024)
    state.download = lambda index: [b"G" + b"x" * 1023, b"x"]
    with pytest.raises(PipelineError, match="Kick response exceeds budget"):
        value.segment(ROOT + "0.ts")
    assert state.calls == state.finished == [0]
    assert value.used <= value.max_bytes
    assert value._reserved == 0
    assert not list(value.cache.iterdir())


@pytest.mark.parametrize("operation", ["prefetch", "retrieve"])
def test_one_worker_keeps_sequential_order_and_reuses_cache(
    make_vod, tmp_path, operation
):
    value, state = make_vod(count=8, workers=1)
    if operation == "prefetch":
        value.prefetch(0, 8)
    else:
        value.retrieve(tmp_path / "video.mp4")
    assert state.calls == list(range(8))
    assert len(state.sessions) == 2
    assert value.segment(ROOT + "0.ts").read_bytes() == b"G\x00"
    assert state.calls == list(range(8))


@pytest.mark.parametrize("status", [206, 302, 400, 401, 404, 422, 505])
def test_http_errors_close_response_and_do_not_retry(make_vod, status):
    value, state = make_vod(count=1)
    state.statuses[0] = status
    with pytest.raises(PipelineError, match=f"HTTP {status}; failed after 1 attempts"):
        value.prefetch(0, 1)
    assert state.calls == state.finished == [0]
    assert value.used == value._reserved == 0


@pytest.mark.parametrize("status", [429, 500, 501, 502, 503, 504, 403])
def test_transient_http_retries_are_bounded_and_release_budget(
    make_vod, monkeypatch, status
):
    value, state = make_vod(count=1, max_bytes=1024)
    state.statuses[0] = status
    sleeps = []

    def sleep(seconds):
        assert value.used == value._reserved == 0
        assert value._lock.acquire(blocking=False)
        value._lock.release()
        sleeps.append(seconds)

    monkeypatch.setattr(km.time, "sleep", sleep)
    attempts = 2 if status == 403 else 4
    with pytest.raises(
        PipelineError, match=f"HTTP {status}; failed after {attempts} attempts"
    ):
        value.segment(ROOT + "0.ts")
    assert sleeps == ([10] if status == 403 else [5, 15, 45])
    assert state.calls == state.finished == [0] * attempts
    assert value.used == value._reserved == 0
    assert not list(value.cache.iterdir())


@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError])
@pytest.mark.parametrize("during_body", [False, True])
def test_transport_retry_settles_each_attempt_and_charges_only_completed_body(
    make_vod, monkeypatch, failure, during_body
):
    value, state = make_vod(count=1, max_bytes=1024)
    session = value._new_session()
    monkeypatch.setattr(value, "_worker_session", lambda: session)
    original = session.get
    calls, sleeps = [], []

    def get(url, **options):
        calls.append(url)
        if len(calls) <= 3:
            if not during_body:
                raise failure("synthetic connection failure")
            response = original(url, **options)

            def chunks():
                yield b"Gpartial"
                raise failure("synthetic connection failure")

            response.iter_content = chunks
            return response
        return original(url, **options)

    def sleep(seconds):
        assert value.used == value._reserved == 0
        sleeps.append(seconds)

    monkeypatch.setattr(session, "get", get)
    monkeypatch.setattr(km.time, "sleep", sleep)
    try:
        assert value.segment(ROOT + "0.ts").read_bytes() == b"G\x00"
    finally:
        session.close()
    assert len(calls) == 4
    assert sleeps == [5, 15, 45]
    assert value.used == 2 and value._reserved == 0


def test_retry_wait_allows_another_thread_to_use_session_and_budget(
    make_vod, monkeypatch
):
    value, state = make_vod(count=2, max_bytes=1024)
    state.statuses[0] = 503

    def sleep(seconds):
        assert seconds == 5
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(value.segment, ROOT + "1.ts").result(timeout=5).is_file()
            assert pool.submit(value.fetch, ROOT + "master.m3u8", 1024).result(
                timeout=5
            )
        state.statuses[0] = 200

    monkeypatch.setattr(km.time, "sleep", sleep)
    assert value.segment(ROOT + "0.ts").is_file()
    assert value.used == 4 and value._reserved == 0


def test_retry_reserves_only_remaining_budget_after_other_download(
    make_vod, monkeypatch
):
    value, state = make_vod(count=2, max_bytes=1024)
    state.statuses[0] = [503, 200]
    state.download = lambda index: b"G" + b"x" * (1021 if index else 1)

    def sleep(seconds):
        assert value._reserved == 0
        assert value.segment(ROOT + "1.ts").stat().st_size == 1022

    monkeypatch.setattr(km.time, "sleep", sleep)
    assert value.segment(ROOT + "0.ts").stat().st_size == 2
    assert value.used == 1024 and value._reserved == 0


def test_403_is_retried_only_once_even_between_transient_errors(make_vod, monkeypatch):
    value, state = make_vod(count=1)
    state.statuses[0] = [403, 503, 403]
    sleeps = []
    monkeypatch.setattr(km.time, "sleep", sleeps.append)
    with pytest.raises(PipelineError, match="HTTP 403; failed after 3 attempts"):
        value.segment(ROOT + "0.ts")
    assert sleeps == [10, 15]
    assert value.used == value._reserved == 0


@pytest.mark.parametrize(
    "header,expected", [("2", 2), ("120", 60), ("-1", 0), ("invalid", 5), ("nan", 5)]
)
@pytest.mark.parametrize("status", [429, 503, 403])
def test_retry_after_controls_wait(make_vod, monkeypatch, header, expected, status):
    value, _ = make_vod(count=1)
    calls, sleeps, closed = [], [], []

    def get(url, **options):
        calls.append(url)
        return SimpleNamespace(
            status_code=status if len(calls) == 1 else 200,
            headers={"Retry-After": header},
            iter_content=lambda: [b"synthetic"],
            close=lambda: closed.append(True),
        )

    monkeypatch.setattr(value.session, "get", get)
    monkeypatch.setattr(km.time, "sleep", sleeps.append)
    assert value.fetch(ROOT + "synthetic?private=synthetic", 1024) == b"synthetic"
    if status == 403 and header in {"invalid", "nan"}:
        expected = 10
    assert sleeps == [expected] and len(closed) == 2


def test_metadata_retry_wait_does_not_hold_shared_session_lock(make_vod, monkeypatch):
    value, _ = make_vod(count=1)
    calls = []

    def get(url, **options):
        calls.append(url)
        return SimpleNamespace(
            status_code=503 if len(calls) == 1 else 200,
            headers={},
            iter_content=lambda: [b"synthetic"],
            close=lambda: None,
        )

    def sleep(seconds):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(value.fetch, ROOT + "other", 1024).result(timeout=5)

    monkeypatch.setattr(value.session, "get", get)
    monkeypatch.setattr(km.time, "sleep", sleep)
    assert value.fetch(ROOT + "synthetic", 1024) == b"synthetic"
    assert len(calls) == 3


def test_retry_after_accepts_http_date(monkeypatch):
    monkeypatch.setattr(km.time, "time", lambda: 0)
    assert km.retry_after({"Retry-After": "Thu, 01 Jan 1970 00:00:30 GMT"}, 5) == 30


def test_retry_failure_keeps_last_status_without_url_or_exception_detail(
    make_vod, monkeypatch
):
    value, _ = make_vod(count=1)
    calls = []

    def get(url, **options):
        calls.append(url)
        if len(calls) > 1:
            raise TimeoutError(url)
        return SimpleNamespace(status_code=503, headers={}, close=lambda: None)

    monkeypatch.setattr(value.session, "get", get)
    monkeypatch.setattr(km.time, "sleep", lambda seconds: None)
    with pytest.raises(PipelineError) as caught:
        value.fetch(ROOT + "synthetic?private=synthetic", 1024)
    assert str(caught.value) == "Kick media HTTP 503; failed after 4 attempts"


def test_transport_exhaustion_reports_no_http_response(make_vod, monkeypatch):
    value, _ = make_vod(count=1)
    calls, sleeps = [], []

    def get(url, **options):
        calls.append(url)
        raise TimeoutError(url)

    monkeypatch.setattr(value.session, "get", get)
    monkeypatch.setattr(km.time, "sleep", sleeps.append)
    with pytest.raises(PipelineError) as caught:
        value.fetch(ROOT + "synthetic?private=synthetic", 1024)
    assert str(caught.value) == "Kick media HTTP unavailable; failed after 4 attempts"
    assert len(calls) == 4 and sleeps == [5, 15, 45]


def test_invalid_transport_stream_is_rejected_and_charged(make_vod):
    value, state = make_vod(count=1)
    state.download = lambda index: b"invalid synthetic segment"
    with pytest.raises(PipelineError, match="expected MPEG-TS segment"):
        value.segment(ROOT + "0.ts")
    assert value.used == len(b"invalid synthetic segment")
    assert value._reserved == 0
    assert not list(value.cache.iterdir())


@pytest.mark.parametrize("suffix", [".ts", ".json"])
def test_cache_symlinks_are_rejected_before_any_download(make_vod, tmp_path, suffix):
    value, state = make_vod(count=1)
    key = hashlib.sha256((ROOT + "0.ts").encode()).hexdigest()
    target = tmp_path / "synthetic-cache-target"
    target.write_bytes(b"Gsynthetic")
    (value.cache / (key + suffix)).symlink_to(target)
    with pytest.raises(PipelineError, match="cache symlinks"):
        value.segment(ROOT + "0.ts")
    assert state.calls == []


def test_cache_receipt_hash_is_checked_before_reuse(make_vod):
    value, state = make_vod(count=1)
    path = value.segment(ROOT + "0.ts")
    path.write_bytes(b"Gtampered synthetic data")
    assert value.segment(ROOT + "0.ts").read_bytes() == b"G\x00"
    assert state.calls == [0, 0]
    receipt = json.loads(path.with_suffix(".json").read_text())
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("workers", [0, -1, 1.5, True])
def test_invalid_worker_count_is_rejected(make_vod, workers):
    with pytest.raises(PipelineError, match="invalid worker count"):
        make_vod(workers=workers)
