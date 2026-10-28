"""Bounded Kick VOD HLS retrieval with original-stream time coordinates."""

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from email.utils import parsedate_to_datetime
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .media import ASR_SAMPLE_RATE
from .storage import TIME_TOLERANCE, PipelineError, digest, process, write_json


RANGE_FORMAT = "segment-remux-v1"


class HTTPFailure(Exception):
    def __init__(self, status, headers):
        self.status, self.headers = status, headers


def retry_after(headers, default):
    value = headers.get("Retry-After")
    if value is not None:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                return default
        if math.isfinite(seconds):
            return min(60, max(0, seconds))
    return default


def extract_audio(source, output, start, duration):
    """Decode locally with PyAV; timestamps retain the selected TS interval's clock."""
    import av

    try:
        with (
            av.open(str(source), options={"protocol_whitelist": "file,pipe"}) as media,
            av.open(
                str(output), "w", format="mp4", options={"movflags": "+faststart"}
            ) as target,
        ):
            if not media.streams.audio:
                raise PipelineError("valid audio is required")
            audio = media.streams.audio[0]
            origin = (
                media.start_time / av.time_base
                if media.start_time is not None
                else float((audio.start_time or 0) * audio.time_base)
            )
            stream = target.add_stream("aac", rate=ASR_SAMPLE_RATE)
            stream.layout, stream.bit_rate = "mono", 48000
            resampler = av.AudioResampler(
                format="fltp", layout="mono", rate=ASR_SAMPLE_RATE
            )
            written = 0

            def encode(frame):
                nonlocal written
                if frame.time is None:
                    raise PipelineError("audio timestamp is required")
                relative = frame.time - origin
                first = max(0, math.ceil((start - relative) * ASR_SAMPLE_RATE - 1e-6))
                last = min(
                    frame.samples,
                    math.ceil((start + duration - relative) * ASR_SAMPLE_RATE - 1e-6),
                )
                if first >= last:
                    return
                trimmed = av.AudioFrame.from_ndarray(
                    frame.to_ndarray()[:, first:last].copy(),
                    format="fltp",
                    layout="mono",
                )
                trimmed.sample_rate, trimmed.time_base = (
                    ASR_SAMPLE_RATE,
                    Fraction(1, ASR_SAMPLE_RATE),
                )
                trimmed.pts = written
                written += trimmed.samples
                for packet in stream.encode(trimmed):
                    target.mux(packet)

            for frame in media.decode(audio):
                for converted in resampler.resample(frame):
                    encode(converted)
                if frame.time is not None and frame.time - origin >= start + duration:
                    break
            for converted in resampler.resample(None):
                encode(converted)
            if not written:
                raise PipelineError("valid audio is required")
            for packet in stream.encode(None):
                target.mux(packet)
        with av.open(str(output)) as media:
            if media.duration is None:
                raise PipelineError("audio duration is required")
            return media.duration / av.time_base
    except av.FFmpegError:
        raise PipelineError("PyAV audio extraction failed") from None


def _metadata_reason(error):
    """Stable reason codes, without forwarding response bodies or URLs."""
    match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(error))
    if match:
        return "http_" + match[1]
    if "HTTP unavailable" in str(error) or isinstance(
        error, (ConnectionError, TimeoutError)
    ):
        return "connection_error"
    return "invalid_metadata"


def _metadata_failure(api_reason, page_reason):
    failure = PipelineError(
        "Kick VOD identity/metadata failed (api="
        + api_reason
        + "; page="
        + page_reason
        + ")"
    )
    failure.reasons = {"api": api_reason, "page": page_reason}
    return failure


class VideoMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active, self.parts, self.videos = False, [], []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = dict(attrs).get("type") == "application/ld+json"
            self.parts = []

    def handle_data(self, text):
        if self.active:
            self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.active = False
            try:
                value = json.loads("".join(self.parts))
            except ValueError:
                return
            if isinstance(value, dict):
                nodes = value.get("@graph", [value])
                if isinstance(nodes, list):
                    self.videos += [
                        v
                        for v in nodes
                        if isinstance(v, dict) and v.get("@type") == "VideoObject"
                    ]


def canonical(url):
    if not re.fullmatch(
        r"https://kick\.com/[A-Za-z0-9_-]+/videos/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        url,
    ):
        raise PipelineError("expected canonical Kick VOD URL")
    return url


def same_vod(value, url):
    """Compare a page URL with the canonical VOD URL, ignoring letter case."""
    return isinstance(value, str) and value.lower() == url.lower()


def stream_url(value):
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == "stream.kick.com"


def nested(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def child(base, reference, root):
    target = urljoin(base, reference)
    p = urlsplit(target)
    if (
        p.scheme != "https"
        or p.hostname != "stream.kick.com"
        or p.port not in (None, 443)
        or p.username
        or p.password
        or p.query
        or p.fragment
        or not p.path.startswith(root)
        or "%" in p.path
        or "\\" in p.path
        or any(part in {".", ".."} for part in p.path.split("/"))
    ):
        raise PipelineError("media URL must stay inside the public archive")
    return target


def parse_media(text, base, root):
    if not text.startswith("#EXTM3U"):
        raise PipelineError("invalid HLS playlist")
    if any(
        tag in text
        for tag in (
            "#EXT-X-KEY",
            "#EXT-X-BYTERANGE",
            "#EXT-X-MAP",
            "#EXT-X-DISCONTINUITY",
        )
    ):
        raise PipelineError(
            "encrypted, byte-range, fMP4 or discontinuous HLS is unsupported"
        )
    result, start, pending = [], 0.0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            pending = float(line.split(":", 1)[1].split(",")[0])
            if not math.isfinite(pending) or not 0 < pending <= 60:
                raise PipelineError("invalid HLS duration")
        elif line and not line.startswith("#"):
            if pending is None:
                raise PipelineError("missing HLS duration")
            result.append(
                {"start": start, "end": start + pending, "url": child(base, line, root)}
            )
            start += pending
            pending = None
    if not result or len(result) > 20000:
        raise PipelineError("empty or oversized HLS playlist")
    return result


class KickVOD:
    def __init__(self, url, cache, max_bytes=2 * 1024**3, max_seconds=43200, workers=6):
        self.url = canonical(url)
        if not 1024 <= max_bytes <= 16 * 1024**3 or not 1 <= max_seconds <= 86400:
            raise PipelineError("invalid byte/duration budget")
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise PipelineError("invalid worker count")
        self.cache = Path(cache).resolve()
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes, self.max_seconds, self.used = max_bytes, max_seconds, 0
        self._lock = threading.RLock()
        self._budget = threading.Condition(self._lock)
        self._session_lock = threading.Lock()
        self._local = threading.local()
        self._sessions, self._inflight = [], {}
        self._reserved, self._closed = 0, False
        self.workers = workers
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self.ca = (
            os.environ.get("SSL_CERT_FILE")
            or os.environ.get("REQUESTS_CA_BUNDLE")
            or ssl.get_default_verify_paths().cafile
        )
        from curl_cffi import requests

        errors = getattr(requests, "exceptions", None)
        self._transport_errors = (ConnectionError, TimeoutError) + (
            (errors.ConnectionError, errors.Timeout) if errors is not None else ()
        )
        self._session_factory = requests.Session
        self.session = self._new_session()
        try:
            try:
                found = self._api_video()
            except PipelineError as api_error:
                api_reason = _metadata_reason(api_error)
                # A missing metadata endpoint can use the public VOD page.
                # Do not turn refusals, rate limits, transport failures or
                # contradictory identities into another retrieval attempt.
                if api_reason != "http_404":
                    raise _metadata_failure(api_reason, "not_attempted") from None
                try:
                    found = self._page_video()
                    if found is None:
                        raise PipelineError("VOD identity missing or ambiguous")
                except (PipelineError, UnicodeError, ValueError) as page_error:
                    raise _metadata_failure(
                        api_reason, _metadata_reason(page_error)
                    ) from None
            if found is None:
                raise PipelineError("VOD identity missing or ambiguous")
            self.metadata, self.master = found
            self.root = urlsplit(self.master).path.rsplit("/", 1)[0] + "/"
            child(self.master, self.master, self.root)
            master = self.fetch(self.master, 256 * 1024).decode()
            self.variants = []
            lines = master.splitlines()
            for index, line in enumerate(lines[:-1]):
                if line.startswith("#EXT-X-STREAM-INF:"):
                    res = re.search(r"RESOLUTION=\d+x(\d+)", line)
                    bw = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line.split(":", 1)[1])
                    self.variants.append(
                        {
                            "height": int(res[1]) if res else 0,
                            "bandwidth": int(bw[1]) if bw else 0,
                            "url": child(
                                self.master, lines[index + 1].strip(), self.root
                            ),
                        }
                    )
            # Prefer an advertised audio-only rendition for full-stream analysis.
            for line in lines:
                if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                    uri = re.search(r'URI="([^"]+)"', line)
                    if uri:
                        self.variants.append(
                            {
                                "height": 0,
                                "bandwidth": 0,
                                "url": child(self.master, uri[1], self.root),
                            }
                        )
            if not self.variants:
                raise PipelineError("no HLS renditions")
        except BaseException:
            self.close()
            raise

    def _api_video(self):
        # The JSON API is keyed by the VOD uuid; its channel slug must match the page URL.
        channel, _, uuid = urlsplit(self.url).path.strip("/").split("/")
        body = self.fetch(f"https://kick.com/api/v1/video/{uuid}", 1024**2)
        try:
            data = json.loads(body)
        except ValueError:
            raise PipelineError("VOD identity missing or ambiguous") from None
        slug = nested(data, "livestream", "channel", "slug")
        source = nested(data, "source")
        if (
            not isinstance(slug, str)
            or slug.lower() != channel.lower()
            or not stream_url(source)
        ):
            raise PipelineError("VOD identity missing or ambiguous")
        metadata = {}
        title = nested(data, "livestream", "session_title")
        started = nested(data, "livestream", "start_time")
        length = nested(data, "livestream", "duration")
        if isinstance(title, str):
            metadata["name"] = title
        if isinstance(started, str):
            metadata["uploadDate"] = started
        if isinstance(length, int) and not isinstance(length, bool):
            # The API reports milliseconds.
            metadata["duration"] = length / 1000
        return metadata, source

    def _page_video(self):
        parser = VideoMetadata()
        parser.feed(self.fetch(self.url, 2 * 1024**2).decode("utf-8"))
        matches = [v for v in parser.videos if same_vod(v.get("url"), self.url)]
        if len(matches) != 1:
            return None
        v = matches[0]
        metadata = {k: v[k] for k in ("name", "duration", "uploadDate") if k in v}
        return metadata, v.get("contentUrl", "")

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._session_lock:
            self.session.close()
        for session in self._sessions:
            session.close()

    def _new_session(self):
        return self._session_factory(
            impersonate="chrome",
            headers={"Referer": "https://kick.com/"},
            verify=self.ca or True,
        )

    def _worker_session(self):
        if not hasattr(self._local, "session"):
            self._local.session = self._new_session()
            with self._lock:
                self._sessions.append(self._local.session)
        return self._local.session

    def fetch(self, url, limit):
        return self._fetch(self.session, url, limit, lock=self._session_lock)

    def _fetch(self, session, url, limit, *, segment=False, lock=None):
        # The production policy forbids automatically retrying media retrieval.
        try:
            return self._fetch_once(session, url, limit, segment=segment, lock=lock)
        except HTTPFailure as error:
            last_status = error.status
        except self._transport_errors:
            last_status = "unavailable"
        raise PipelineError(
            f"Kick media HTTP {last_status}; failed after 1 attempts"
        ) from None

    def _fetch_once(self, session, url, limit, *, segment, lock):
        reserved, body, response = 0, None, None
        if segment:
            with self._budget:
                while (
                    self.max_bytes - self.used - self._reserved <= 0 and self._reserved
                ):
                    self._budget.wait()
                reserved = min(limit, self.max_bytes - self.used - self._reserved)
                if reserved <= 0:
                    raise PipelineError("segment byte budget exhausted")
                self._reserved += reserved
                limit = reserved
        try:
            with lock if lock is not None else nullcontext():
                response = session.get(
                    url, allow_redirects=False, timeout=30, stream=True
                )
                if response.status_code != 200:
                    raise HTTPFailure(
                        response.status_code, getattr(response, "headers", {})
                    )
                chunks, size = [], 0
                for chunk in response.iter_content():
                    size += len(chunk)
                    if size > limit:
                        raise PipelineError("Kick response exceeds budget")
                    chunks.append(chunk)
                body = b"".join(chunks)
                return body
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                if segment:
                    with self._budget:
                        if body is not None:
                            self.used += len(body)
                        self._reserved -= reserved
                        self._budget.notify_all()

    def playlist(self, quality):
        if quality not in {"lowest", "preview", "360p", "720p", "1080p", "best"}:
            raise PipelineError("invalid rendition")
        ordered = sorted(self.variants, key=lambda v: (v["height"], v["bandwidth"]))
        if quality == "lowest":
            selected = ordered[0]
        elif quality == "preview":
            videos = [v for v in ordered if 0 < v["height"] <= 360]
            if not videos:
                videos = [v for v in ordered if v["height"] > 0]
            if not videos:
                raise PipelineError("no video rendition for topic review")
            selected = videos[0]
        else:
            cap = {"360p": 360, "720p": 720, "1080p": 1080, "best": 100000}[quality]
            eligible = [v for v in ordered if 0 < v["height"] <= cap]
            if not eligible:
                eligible = [v for v in ordered if v["height"] > 0][:1]
            if not eligible:
                raise PipelineError("video rendition unavailable")
            selected = eligible[-1]
        text = self.fetch(selected["url"], 2 * 1024**2).decode()
        segments = parse_media(text, selected["url"], self.root)
        if segments[-1]["end"] > self.max_seconds:
            raise PipelineError("archive exceeds configured source duration budget")
        # EVENT is accepted only after the stream has ended; unfinished playlists stop.
        if "#EXT-X-ENDLIST" not in text:
            raise PipelineError(
                "archive playlist is still growing; retry after completion"
            )
        return segments, selected["height"]

    def prefetch(self, start, end):
        """Cache the production 360p interval over HTTP; retrieve() reuses these segments."""
        segments, _ = self.playlist("360p")
        duration = segments[-1]["end"]
        if (
            not all(math.isfinite(v) for v in (start, end))
            or not 0 <= start < end <= duration + TIME_TOLERANCE
        ):
            raise PipelineError("requested range is outside archive")
        self._download_segments(
            [s for s in segments if s["end"] > start and s["start"] < end]
        )

    def segment(self, url):
        return self._segment_future(url).result()

    def _segment_future(self, url):
        with self._lock:
            if self._closed:
                raise PipelineError("Kick VOD is closed")
            future = self._inflight.get(url)
            if future is None:
                future = self._executor.submit(self._segment, url)
                self._inflight[url] = future

                def finished(done):
                    with self._lock:
                        if self._inflight.get(url) is done:
                            del self._inflight[url]

                future.add_done_callback(finished)
            return future

    def _download_segments(self, segments):
        return list(self._stream_segments(segments))

    def _stream_segments(self, segments):
        """Yield each segment's cached path in stream order, as soon as it is ready."""
        # Keep only one worker-width window queued, and stop it on any failure,
        # including failures later than the next segment in stream order.
        pending, paths, failures = {}, {}, []

        def completed(future):
            with self._lock:
                if not future.cancelled() and not failures:
                    error = future.exception()
                    if error is not None:
                        failures.append(error)
                        for other in pending:
                            other.cancel()

        index = emitted = 0
        try:
            while emitted < len(segments):
                with self._lock:
                    if failures:
                        raise failures[0]
                    while len(pending) < self.workers and index < len(segments):
                        url = segments[index]["url"]
                        future = self._segment_future(url)
                        pending[future] = url
                        future.add_done_callback(completed)
                        index += 1
                        if failures:
                            raise failures[0]
                    ready = []
                    while emitted < len(segments) and segments[emitted]["url"] in paths:
                        ready.append(paths.pop(segments[emitted]["url"]))
                        emitted += 1
                    waiting = list(pending)
                for path in ready:
                    yield path
                if emitted == len(segments):
                    break
                done, _ = wait(waiting, return_when=FIRST_COMPLETED)
                with self._lock:
                    for future in done:
                        completed(future)
                    if failures:
                        raise failures[0]
                    for future in done:
                        paths[pending.pop(future)] = future.result()
        except BaseException:
            with self._lock:
                for future in pending:
                    future.cancel()
            raise

    def _segment(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        path, receipt = self.cache / (key + ".ts"), self.cache / (key + ".json")
        with self._budget:
            if path.is_symlink() or receipt.is_symlink():
                raise PipelineError("cache symlinks are not allowed")
            if path.is_file() and receipt.is_file():
                size = path.stat().st_size
                saved = json.loads(receipt.read_text())
                if saved.get("sha256") == digest(path):
                    while (
                        size > self.max_bytes - self.used - self._reserved
                        and self._reserved
                    ):
                        self._budget.wait()
                    if size <= self.max_bytes - self.used:
                        self.used += size
                        return path
        body = self._fetch(self._worker_session(), url, 64 * 1024**2, segment=True)
        if not body or body[0] != 0x47:
            raise PipelineError("expected MPEG-TS segment")
        with tempfile.NamedTemporaryFile(dir=self.cache, delete=False) as temp:
            temp.write(body)
            name = Path(temp.name)
        name.replace(path)
        write_json(receipt, {"sha256": digest(path), "bytes": len(body)})
        return path

    def _retrieve_audio(self, output, selected, start, end, duration, height):
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".fetch-") as temp:
            product = Path(temp) / "audio.m4a"
            fetched_duration = self._extract_streamed(
                selected, product, start - selected[0]["start"], end - start
            )
            if (
                not math.isfinite(fetched_duration)
                or abs(fetched_duration - (end - start)) > 0.3
            ):
                raise PipelineError("fetched duration mismatch")
            # Hard-link creates the final name exclusively; partial files remain private.
            os.link(product, output)
        return {
            "source_url": self.url,
            "source_start": start,
            "source_end": end,
            "source_duration": duration,
            "rendition_height": height,
            "audio_only": True,
            "segment_count": len(selected),
            "sha256": digest(output),
        }

    def _extract_streamed(self, segments, product, start, duration):
        """Decode segments in order while they download, through a named pipe: no full TS copy."""
        fifo = product.parent / "audio.ts"
        os.mkfifo(fifo, 0o600)
        failures = []

        def write():
            stream = self._stream_segments(segments)
            try:
                with open(fifo, "wb") as pipe:
                    for path in stream:
                        with path.open("rb") as source:
                            shutil.copyfileobj(source, pipe, 1024 * 1024)
            except BrokenPipeError:
                # The reader stops once it has the requested interval; that is not a failure.
                pass
            except BaseException as error:
                failures.append(error)
            finally:
                stream.close()

        thread = threading.Thread(target=write, name="kick-audio-writer", daemon=True)
        thread.start()

        def release():
            # A writer waiting for a reader that never opened the FIFO is released here.
            if thread.is_alive():
                with contextlib.suppress(OSError):
                    open(fifo, "rb").close()
            thread.join(timeout=60)
            if thread.is_alive():
                raise PipelineError("audio download did not stop")

        try:
            fetched = extract_audio(fifo, product, start, duration)
        except BaseException:
            release()
            if failures:
                # A failed download is the cause of any decode error after it.
                raise failures[0] from None
            raise
        release()
        if failures:
            raise failures[0]
        return fetched

    def retrieve(self, output, start=0.0, end=None, quality="720p", audio_only=False):
        output = Path(output).resolve()
        if output.exists():
            raise PipelineError("media output exists")
        segments, height = self.playlist("lowest" if audio_only else quality)
        duration = segments[-1]["end"]
        end = duration if end is None else end
        if (
            not all(math.isfinite(v) for v in (start, end))
            or not 0 <= start < end <= duration + TIME_TOLERANCE
        ):
            raise PipelineError("requested range is outside archive")
        selected = [s for s in segments if s["end"] > start and s["start"] < end]
        remux = not audio_only and quality != "preview"
        start_offset = start - selected[0]["start"] if remux else 0.0
        covered_duration = (
            selected[-1]["end"] - selected[0]["start"] if remux else end - start
        )
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if audio_only:
            return self._retrieve_audio(output, selected, start, end, duration, height)
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".fetch-") as temp:
            ts = Path(temp) / "input.ts"
            with ts.open("wb") as dest:
                for path in self._download_segments(selected):
                    with path.open("rb") as source:
                        import shutil

                        shutil.copyfileobj(source, dest)
            product = Path(temp) / ("audio.m4a" if audio_only else "video.mp4")
            command = [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
            ]
            if remux:
                # Anchor presentation time to the first video frame, not the earliest
                # audio packet or DTS. Keep negative B-frame DTS via MP4 edit lists;
                # make_zero would move the picture away from the segment's zero.
                video = json.loads(
                    process(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=start_time",
                            "-of",
                            "json",
                            ts,
                        ],
                        stage="remux-probe",
                    ).stdout
                )
                video_start = float(video["streams"][0]["start_time"])
                if not math.isfinite(video_start):
                    raise PipelineError("invalid segment video start time")
                command += [
                    "-fflags",
                    "+genpts",
                    "-copyts",
                    "-itsoffset",
                    str(-video_start),
                ]
            else:
                command += ["-ss", str(start - selected[0]["start"])]
            command += [
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(ts),
            ]
            if not remux:
                command += ["-t", str(end - start)]
            if remux:
                command += [
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "disabled",
                    "-use_editlist",
                    "1",
                ]
            else:
                command += [
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-threads",
                    "2",
                    "-c:a",
                    "aac",
                ]
            process(
                [*command, "-movflags", "+faststart", str(product)],
                timeout=max(300, (end - start) * 10),
                stage="remux" if remux else "render",
            )
            info = json.loads(
                process(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        product,
                    ],
                    stage="probe",
                ).stdout
            )
            fetched_duration = float(info["format"]["duration"])
            if (
                not math.isfinite(fetched_duration)
                or abs(fetched_duration - covered_duration) > 0.3
            ):
                raise PipelineError("fetched duration mismatch")
            # Hard-link creates the final name exclusively; partial files remain private.
            os.link(product, output)
        receipt = {
            "source_url": self.url,
            "source_start": start,
            "source_end": end,
            "source_duration": duration,
            "rendition_height": height,
            "audio_only": audio_only,
            "segment_count": len(selected),
            "sha256": digest(output),
        }
        if remux:
            receipt.update(
                media_format=RANGE_FORMAT,
                start_offset=start_offset,
                covered_duration=covered_duration,
            )
        return receipt
