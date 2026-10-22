"""Bounded Kick VOD HLS retrieval with original-stream time coordinates."""

import hashlib
import json
import math
import os
import re
import ssl
import tempfile
import threading
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .storage import PipelineError, digest, process, write_json


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
    def __init__(self, url, cache, max_bytes=2 * 1024**3, max_seconds=14400):
        self.url = canonical(url)
        if not 1024 <= max_bytes <= 16 * 1024**3 or not 1 <= max_seconds <= 86400:
            raise PipelineError("invalid byte/duration budget")
        self.cache = Path(cache).resolve()
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes, self.max_seconds, self.used = max_bytes, max_seconds, 0
        self._lock = threading.RLock()
        self.ca = (
            os.environ.get("SSL_CERT_FILE")
            or os.environ.get("REQUESTS_CA_BUNDLE")
            or ssl.get_default_verify_paths().cafile
        )
        from curl_cffi import requests

        self.session = requests.Session(
            impersonate="chrome",
            headers={"Referer": "https://kick.com/"},
            verify=self.ca or True,
        )
        try:
            parser = VideoMetadata()
            parser.feed(self.fetch(self.url, 2 * 1024**2).decode("utf-8"))
            matches = [v for v in parser.videos if v.get("url") == self.url]
            if len(matches) != 1:
                raise PipelineError("VOD identity missing or ambiguous")
            v = matches[0]
            self.metadata = {
                k: v[k] for k in ("name", "duration", "uploadDate") if k in v
            }
            self.master = v.get("contentUrl", "")
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

    def close(self):
        self.session.close()

    def fetch(self, url, limit):
        # The HTTP session is not shared across threads; prefetch and retrieve take turns.
        with self._lock:
            response = self.session.get(
                url, allow_redirects=False, timeout=30, stream=True
            )
            try:
                if response.status_code != 200:
                    raise PipelineError(
                        f"Kick media HTTP {response.status_code}; no automatic retry"
                    )
                chunks, size = [], 0
                for chunk in response.iter_content():
                    size += len(chunk)
                    if size > limit:
                        raise PipelineError("Kick response exceeds budget")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()

    def playlist(self, quality):
        if quality not in {"lowest", "preview", "720p", "1080p", "best"}:
            raise PipelineError("invalid rendition")
        ordered = sorted(self.variants, key=lambda v: (v["height"], v["bandwidth"]))
        if quality == "lowest":
            selected = ordered[0]
        elif quality == "preview":
            videos = [v for v in ordered if v["height"] > 0]
            if not videos:
                raise PipelineError("no video rendition for topic review")
            selected = videos[0]
        else:
            cap = {"720p": 720, "1080p": 1080, "best": 100000}[quality]
            eligible = [v for v in ordered if 0 < v["height"] <= cap]
            if not eligible:
                raise PipelineError("requested quality unavailable")
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
        """Cache the 1080p segments of one interval over HTTP only; retrieve() then reuses them."""
        segments, _ = self.playlist("1080p")
        duration = segments[-1]["end"]
        if (
            not all(math.isfinite(v) for v in (start, end))
            or not 0 <= start < end <= duration + 0.01
        ):
            raise PipelineError("requested range is outside archive")
        for s in segments:
            if s["end"] > start and s["start"] < end:
                self.segment(s["url"])

    def segment(self, url):
        # One lock covers the cache check, the download and the byte count, so a
        # prefetch and a retrieve for the same URL never download it twice.
        with self._lock:
            return self._segment(url)

    def _segment(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        path, receipt = self.cache / (key + ".ts"), self.cache / (key + ".json")
        remaining = self.max_bytes - self.used
        if path.is_symlink() or receipt.is_symlink():
            raise PipelineError("cache symlinks are not allowed")
        if path.is_file() and receipt.is_file() and path.stat().st_size <= remaining:
            saved = json.loads(receipt.read_text())
            if saved.get("sha256") == digest(path):
                self.used += path.stat().st_size
                return path
        if remaining <= 0:
            raise PipelineError("segment byte budget exhausted")
        body = self.fetch(url, min(64 * 1024**2, remaining))
        self.used += len(body)
        if not body or body[0] != 0x47:
            raise PipelineError("expected MPEG-TS segment")
        with tempfile.NamedTemporaryFile(dir=self.cache, delete=False) as temp:
            temp.write(body)
            name = Path(temp.name)
        name.replace(path)
        write_json(receipt, {"sha256": digest(path), "bytes": len(body)})
        return path

    def retrieve(self, output, start=0.0, end=None, quality="720p", audio_only=False):
        output = Path(output).resolve()
        if output.exists():
            raise PipelineError("media output exists")
        segments, height = self.playlist("lowest" if audio_only else quality)
        duration = segments[-1]["end"]
        end = duration if end is None else end
        if (
            not all(math.isfinite(v) for v in (start, end))
            or not 0 <= start < end <= duration + 0.01
        ):
            raise PipelineError("requested range is outside archive")
        selected = [s for s in segments if s["end"] > start and s["start"] < end]
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".fetch-") as temp:
            ts = Path(temp) / "input.ts"
            with ts.open("wb") as dest:
                for s in selected:
                    with self.segment(s["url"]).open("rb") as source:
                        import shutil

                        shutil.copyfileobj(source, dest)
            product = Path(temp) / ("audio.m4a" if audio_only else "video.mp4")
            command = [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-ss",
                str(start - selected[0]["start"]),
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(ts),
                "-t",
                str(end - start),
            ]
            if audio_only:
                command += [
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "48k",
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
                    ]
                ).stdout
            )
            if abs(float(info["format"]["duration"]) - (end - start)) > 0.3:
                raise PipelineError("fetched duration mismatch")
            # Hard-link creates the final name exclusively; partial files remain private.
            os.link(product, output)
        return {
            "source_url": self.url,
            "source_start": start,
            "source_end": end,
            "source_duration": duration,
            "rendition_height": height,
            "audio_only": audio_only,
            "segment_count": len(selected),
            "sha256": digest(output),
        }
