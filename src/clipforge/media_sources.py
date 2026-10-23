"""Kick, owned HTTPS media and single YouTube/Twitch archives share one interface."""

import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import media
from .kick_media import (
    RANGE_FORMAT,
    KickVOD,
    canonical as kick_canonical,
    extract_audio,
)
from .storage import TIME_TOLERANCE, PipelineError, digest, process

MEDIA_SUFFIXES = {".mp4", ".m4a", ".mp3", ".webm", ".m3u8"}


def provider_error(error):
    """Keep the provider's reason, removing URLs and common credential assignments."""
    detail = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
    detail = re.sub(r"https?://[^\s<>\"']+", "[URL]", detail)
    detail = re.sub(
        r"(?i)\b(cookie|authorization)\s*[:=][^\r\n]*", r"\1=[REDACTED]", detail
    )
    detail = re.sub(
        r"(?i)\b(cookie|authorization|token|signature|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        detail,
    )
    return " ".join(detail.split())[:500]


def source_kind(url):
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or re.search(r"[\s\\\x00-\x1f\x7f]", url)
    ):
        raise PipelineError("expected an HTTPS media or canonical archive URL")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or parts.port == 0
        ):
            raise ValueError
    except ValueError:
        raise PipelineError(
            "expected an HTTPS media or canonical archive URL"
        ) from None
    host = parts.hostname.lower()
    if host == "kick.com":
        kick_canonical(url)
        return "kick"
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        identity = (
            parts.path[1:]
            if host == "youtu.be"
            else parse_qs(parts.query).get("v", [""])[0]
            if parts.path == "/watch"
            else re.sub(r"^/(shorts|live)/", "", parts.path)
        )
        if parts.port not in (None, 443) or not re.fullmatch(
            r"[A-Za-z0-9_-]{11}", identity
        ):
            raise PipelineError("expected one YouTube video URL")
        return "youtube"
    if host in {"twitch.tv", "www.twitch.tv", "clips.twitch.tv"}:
        pattern = (
            r"/[A-Za-z0-9_-]+"
            if host == "clips.twitch.tv"
            else r"/(?:videos/[0-9]+|[A-Za-z0-9_]+/clip/[A-Za-z0-9_-]+)"
        )
        if parts.port not in (None, 443) or not re.fullmatch(pattern, parts.path):
            raise PipelineError("expected one Twitch archive or clip URL")
        return "twitch"
    if Path(unquote(parts.path)).suffix.lower() in MEDIA_SUFFIXES:
        return "direct"
    raise PipelineError(
        "expected canonical Kick, YouTube, Twitch or HTTPS mp4/m4a/mp3/webm/m3u8 URL"
    )


def canonical(url):
    source_kind(url)
    return url


def audio_only_url(url):
    return source_kind(url) == "direct" and Path(
        unquote(urlsplit(url).path)
    ).suffix.lower() in {".m4a", ".mp3"}


def open_source(url, cache, *, kick_factory=KickVOD, **budgets):
    factory = kick_factory if source_kind(url) == "kick" else RemoteMedia
    return factory(url, cache, **budgets)


class QuietLogger:
    # Provider diagnostics can include signed URLs; retain only our fixed reason messages.
    def debug(self, message):
        pass

    info = warning = error = debug


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise PipelineError("media redirects must use HTTPS")
        return super().redirect_request(request, fp, code, message, headers, newurl)


class RemoteMedia:
    def __init__(
        self,
        url,
        cache,
        max_bytes=2 * 1024**3,
        max_seconds=43200,
        workers=1,
        cookies_file=None,
    ):
        self.url = canonical(url)
        if (
            type(max_bytes) is not int
            or not 1024 <= max_bytes <= 16 * 1024**3
            or not 1 <= max_seconds <= 86400
        ):
            raise PipelineError("invalid byte/duration budget")
        self.cache = Path(cache).resolve()
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes, self.max_seconds, self.used = max_bytes, max_seconds, 0
        self._lock = threading.RLock()
        self._downloads, self._temporaries = {}, []
        self.has_video = not audio_only_url(url)
        self._cookiefile = None
        if source_kind(url) == "youtube":
            from .youtube_auth import cookie_copy

            temporary, self._cookiefile = cookie_copy(cookies_file)
            if temporary is not None:
                self._temporaries.append(temporary)
        elif cookies_file is not None:
            raise PipelineError("--cookies-file requires a YouTube source")

    def close(self):
        for temporary in self._temporaries:
            temporary.cleanup()
        self._temporaries.clear()
        self._downloads.clear()

    def prefetch(self, start, end):
        # Retrieval owns the download budget; speculative video requests are unnecessary.
        pass

    def _options(self, audio_only, quality):
        options = {
            "quiet": True,
            "no_warnings": True,
            "logger": QuietLogger(),
            "noplaylist": True,
            "playlistend": 1,
            "ignoreerrors": False,
            "retries": 0,
            "fragment_retries": 0,
            "extractor_retries": 0,
            "socket_timeout": 20,
            "concurrent_fragment_downloads": 1,
            "hls_prefer_native": True,
            "skip_unavailable_fragments": False,
            "cachedir": False,
            "overwrites": False,
            "enable_file_urls": False,
            "format": "bestaudio/worst"
            if audio_only
            else "worst[height<=360]/worst"
            if quality == "preview"
            else "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "merge_output_format": "mkv",
            "js_runtimes": {"node": {}},
        }

        if self._cookiefile:
            options["cookiefile"] = self._cookiefile
        return options

    def _validate(self, info):
        if (
            not isinstance(info, dict)
            or info.get("_type", "video") != "video"
            or info.get("entries") is not None
        ):
            raise PipelineError(
                "one media file or archive is required; playlists are unsupported"
            )
        if info.get("is_live") or info.get("live_status") in {
            "is_live",
            "is_upcoming",
            "post_live",
        }:
            raise PipelineError(
                "a finished archive is required; live streams are unsupported"
            )
        duration = info.get("duration")
        if duration is not None and (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or not 0 < duration <= self.max_seconds
        ):
            raise PipelineError(
                "source duration exceeds the analysis budget or is invalid"
            )
        formats = info.get("requested_formats") or [info]
        for selected in formats:
            protocol = selected.get("protocol")
            if (
                protocol not in {"https", "m3u8_native"}
                or urlsplit(selected.get("url", "")).scheme != "https"
            ):
                raise PipelineError("selected media must use HTTPS or HTTPS HLS")
            if selected.get("has_drm"):
                raise PipelineError("protected media is unsupported")
            size = selected.get("filesize")
            if isinstance(size, (int, float)) and size > self.max_bytes - self.used:
                raise PipelineError("media exceeds the transport byte budget")
        available = info.get("formats") or formats
        self.has_video = (
            any(
                row.get("vcodec") not in (None, "none") or row.get("height")
                for row in available
            )
            if any("vcodec" in row or "height" in row for row in available)
            else self.has_video
        )

    def _check_hls(self, info):
        for selected in info.get("requested_formats") or [info]:
            if selected.get("protocol") != "m3u8_native":
                continue
            try:
                request = Request(
                    selected["url"],
                    headers={
                        key: value
                        for key, value in selected.get("http_headers", {}).items()
                        if key.lower() in {"user-agent", "referer", "origin"}
                    },
                )
                with build_opener(HTTPSRedirects()).open(
                    request, timeout=20
                ) as response:
                    data = response.read(1024 * 1024 + 1)
                    if urlsplit(response.url).scheme != "https":
                        raise PipelineError("HLS playlist must use HTTPS")
            except PipelineError:
                raise
            except Exception:
                raise PipelineError("HLS playlist could not be read") from None
            if (
                len(data) > 1024 * 1024
                or not data.startswith(b"#EXTM3U")
                or b"#EXT-X-ENDLIST" not in data
            ):
                raise PipelineError(
                    "a finite HLS media playlist with ENDLIST is required"
                )

    def metadata(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        try:
            with YoutubeDL(self._options(True, "lowest")) as downloader:
                info = downloader.extract_info(self.url, download=False)
                self._validate(info)
                self._check_hls(info)
                return {"duration": info.get("duration"), "has_video": self.has_video}
        except PipelineError:
            raise
        except DownloadError as error:
            raise PipelineError(
                "media metadata acquisition failed (yt-dlp): " + provider_error(error)
            ) from None

    def _download(self, audio_only, quality):
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        key = (audio_only, quality)
        if key in self._downloads:
            return self._downloads[key]
        temporary = tempfile.TemporaryDirectory(prefix="media-", dir=self.cache)
        self._temporaries.append(temporary)
        directory = Path(temporary.name)
        observed, deadline = {}, time.monotonic() + 1200

        def progress(event):
            name, amount = event.get("filename", ""), event.get("downloaded_bytes", 0)
            if time.monotonic() > deadline:
                raise PipelineError("media download exceeded its time budget")
            if type(amount) is int:
                self.used += max(0, amount - observed.get(name, 0))
                observed[name] = amount
            if self.used > self.max_bytes:
                raise PipelineError("media exceeds the transport byte budget")

        options = self._options(audio_only, quality)
        options.update(
            outtmpl=str(directory / "source.%(ext)s"),
            progress_hooks=[progress],
            max_filesize=self.max_bytes - self.used,
            restrictfilenames=True,
        )
        phase = "metadata"
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(self.url, download=False)
                self._validate(info)
                self._check_hls(info)
                phase = "download"
                downloader.process_ie_result(info, download=True)
        except PipelineError:
            raise
        except DownloadError as error:
            raise PipelineError(
                "media download failed (yt-dlp; phase="
                + phase
                + "): "
                + provider_error(error)
            ) from None
        files = [
            path
            for path in directory.glob("source.*")
            if path.suffix not in {".part", ".ytdl", ".temp"}
            and not re.search(r"\.f[^.]+\.", path.name)
        ]
        if (
            len(files) != 1
            or files[0].is_symlink()
            or not 0 < files[0].stat().st_size <= self.max_bytes
        ):
            raise PipelineError("media download is missing, oversized or incomplete")
        path = files[0]
        info = json.loads(
            process(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    *media.PROTOCOLS,
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    path,
                ],
                stage="source-probe",
            ).stdout
        )
        duration = float(info.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or not 0 < duration <= self.max_seconds:
            raise PipelineError(
                "source duration exceeds the analysis budget or is unavailable"
            )
        if not any(row.get("codec_type") == "audio" for row in info.get("streams", [])):
            raise PipelineError("valid audio is required")
        video = next(
            (
                row
                for row in info["streams"]
                if row.get("codec_type") == "video"
                and not row.get("disposition", {}).get("attached_pic")
            ),
            None,
        )
        details = {
            "duration": duration,
            "height": video.get("height") if video else None,
            "video_start": float(video.get("start_time", 0)) if video else 0,
            "has_video": video is not None,
        }
        # Audio-only YouTube renditions do not describe the source's available video.
        if not audio_only or source_kind(self.url) == "direct":
            self.has_video = details["has_video"]
        self._downloads[key] = (path, details)
        return path, details

    def retrieve(self, output, start=0.0, end=None, quality="720p", audio_only=False):
        with self._lock:
            return self._retrieve(Path(output), start, end, quality, audio_only)

    def _retrieve(self, output, start, end, quality, audio_only):
        if output.exists() or output.is_symlink():
            raise PipelineError("media output exists")
        source, info = self._download(audio_only, quality)
        duration = info["duration"]
        end = duration if end is None else end
        if (
            any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (start, end)
            )
            or not 0 <= start < end <= duration + TIME_TOLERANCE
        ):
            raise PipelineError("requested range is outside archive")
        if not audio_only and not info["has_video"]:
            raise PipelineError(
                "audio-only sources cannot produce video previews or renders"
            )
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        remux = not audio_only and quality != "preview"
        with tempfile.TemporaryDirectory(
            prefix=".fetch-", dir=output.parent
        ) as temporary:
            product = Path(temporary) / ("audio.m4a" if audio_only else "video.mp4")
            if audio_only:
                extract_audio(source, product, start, end - start)
            else:
                command = ["ffmpeg", "-nostdin", "-v", "error", "-n"]
                if remux:
                    command += ["-copyts", "-itsoffset", str(-info["video_start"])]
                else:
                    command += ["-ss", str(start)]
                command += [
                    *media.PROTOCOLS,
                    "-i",
                    source,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                ]
                if remux:
                    command += [
                        "-c",
                        "copy",
                        "-avoid_negative_ts",
                        "disabled",
                        "-use_editlist",
                        "1",
                    ]
                else:
                    command += [
                        "-t",
                        str(end - start),
                        "-vf",
                        "scale=-2:'min(360,ih)'",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "24",
                        "-threads",
                        "2",
                        "-c:a",
                        "aac",
                    ]
                process(
                    [*command, "-movflags", "+faststart", product],
                    timeout=max(300, duration * 2),
                    stage="source-remux" if remux else "source-preview",
                )
            os.link(product, output)
        receipt = {
            "source_url": self.url,
            "source_start": start,
            "source_end": end,
            "source_duration": duration,
            "rendition_height": info["height"],
            "audio_only": audio_only,
            "sha256": digest(output),
            "transport": "yt-dlp native download; progressive files may transfer the whole source",
            "high_quality_video_fetched": bool(
                audio_only and info["has_video"] and (info["height"] or 0) >= 720
            ),
        }
        if remux:
            # Retain the full source clock instead of pretending a keyframe cut is exact.
            receipt.update(
                media_format=RANGE_FORMAT, start_offset=start, covered_duration=duration
            )
        return receipt
