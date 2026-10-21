"""Free-image search with licence filtering, bounded parallel download, and a source ledger.

Only images whose licence allows reuse with attribution (CC0, public domain, CC BY,
CC BY-SA) are kept. Every downloaded file gets a ledger entry with its page URL,
credit line and licence; a visual without a complete entry is never rendered.
"""

import html
import ipaddress
import json
import re
import socket
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from PIL import Image

from .storage import PipelineError, digest

USER_AGENT = "ClipForge/0.3 (https://github.com/nyu1791-collab/ClipForge)"
MAX_BYTES = 8 * 1024 * 1024
MAX_WORKERS = 4
TIMEOUT = 20
IMAGE_SUFFIXES = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
ALLOWED_LICENSE = re.compile(
    r"^(CC0( 1\.0)?|Public domain|CC BY(-SA)?( [0-9]\.[0-9])?)$", re.IGNORECASE
)
OPENVERSE_LICENSES = "cc0,pdm,by,by-sa"
COMMONS_QUERY_SUFFIX = " filetype:bitmap"


def _strip_html(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def _public_https(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PipelineError("only https image URLs are accepted")
    try:
        infos = socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP)
    except OSError as error:
        raise PipelineError("image host could not be resolved") from error
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not address.is_global:
            raise PipelineError("image host resolves to a non-public address")
    return url


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    """Each redirect target is checked like the first URL, so no hop leaves the public web."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_PublicRedirects)


def _retry_after(headers):
    try:
        return min(30, max(1, int(headers.get("Retry-After", "5"))))
    except (TypeError, ValueError):
        return 5


def _get(url):
    _public_https(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with _OPENER.open(request, timeout=TIMEOUT) as response:
                data = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt == 0:
                time.sleep(_retry_after(error.headers))
                continue
            raise PipelineError(
                f"image search request failed with HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, OSError) as error:
            raise PipelineError("image search request failed") from error
        if len(data) > MAX_BYTES:
            raise PipelineError("response is larger than the allowed size")
        return data
    raise PipelineError("image search request was rate limited")


def _json(url):
    try:
        return json.loads(_get(url))
    except ValueError as error:
        raise PipelineError("image search returned unreadable data") from error


def search_commons(query, limit):
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query + COMMONS_QUERY_SUFFIX,
        "gsrnamespace": 6,
        "gsrlimit": limit,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime",
        "iiurlwidth": 1280,
        "format": "json",
        "formatversion": 2,
    }
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    data = _json(url)
    results = []
    for page in data.get("query", {}).get("pages", []):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = _strip_html(meta.get("LicenseShortName", {}).get("value"))
        if not ALLOWED_LICENSE.match(license_name):
            continue
        if info.get("mime") not in {"image/jpeg", "image/png", "image/webp"}:
            continue
        artist = _strip_html(meta.get("Artist", {}).get("value")) or "unknown author"
        results.append(
            {
                "provider": "wikimedia-commons",
                "image_url": info.get("thumburl") or info.get("url"),
                "source_url": info.get("descriptionurl"),
                "source_credit": f"{artist} / {license_name} / Wikimedia Commons",
                "license": license_name,
                "license_url": _strip_html(meta.get("LicenseUrl", {}).get("value")),
                "query": query,
            }
        )
    return results[:limit]


def search_openverse(query, limit):
    params = {
        "q": query,
        "license": OPENVERSE_LICENSES,
        "license_type": "commercial",
        "page_size": limit,
    }
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    data = _json(url)
    results = []
    for item in data.get("results", []):
        code = str(item.get("license", "")).lower()
        if code not in {"cc0", "pdm", "by", "by-sa"} or not item.get("url"):
            continue
        if code == "cc0":
            license_name = "CC0"
        elif code == "pdm":
            license_name = "Public domain"
        else:
            version = item.get("license_version", "")
            license_name = f"CC {code.upper()} {version}".strip()
        creator = item.get("creator") or "unknown author"
        results.append(
            {
                "provider": "openverse",
                "image_url": item["url"],
                "source_url": item.get("foreign_landing_url") or item["url"],
                "source_credit": f"{creator} / {license_name} / Openverse",
                "license": license_name,
                "license_url": item.get("license_url") or "",
                "query": query,
            }
        )
    return results[:limit]


def search(query, sources, limit):
    """Search the selected providers in order; a failing provider does not stop the others."""
    found = []
    for source in sources:
        try:
            if source == "commons":
                found += search_commons(query, limit)
            elif source == "openverse":
                found += search_openverse(query, limit)
        except PipelineError:
            continue
    return found[:limit]


def check_image(path):
    """Format of a decodable JPEG, PNG or WebP file; anything else is refused."""
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            fmt = image.format
    except Exception as error:
        raise PipelineError("file is not a valid image") from error
    if fmt not in IMAGE_SUFFIXES:
        raise PipelineError("unsupported image format")
    return fmt


def _download_one(item, directory, index):
    data = _get(item["image_url"])
    scratch = directory / f"image-{index:02}.tmp"
    scratch.write_bytes(data)
    try:
        fmt = check_image(scratch)
    except PipelineError:
        scratch.unlink(missing_ok=True)
        raise
    final = directory / f"image-{index:02}{IMAGE_SUFFIXES[fmt]}"
    scratch.replace(final)
    return {
        "id": f"image-{index:02}",
        "file": final.name,
        "kind": "photo",
        "sha256": digest(final),
        "provider": item["provider"],
        "source_url": item["source_url"],
        "source_credit": item["source_credit"],
        "license": item["license"],
        "license_url": item["license_url"],
        "query": item["query"],
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def download_all(items, directory):
    """Download with at most MAX_WORKERS concurrent transfers; failures are skipped."""
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(_download_one, item, directory, index)
            for index, item in enumerate(items, 1)
        ]
        for future in futures:
            try:
                entries.append(future.result())
            except PipelineError:
                continue
    return entries


def validate_entry(entry):
    """A ledger entry is complete only with source, credit and licence."""
    required = ("file", "sha256", "source_url", "source_credit", "license")
    if any(not entry.get(key) for key in required):
        raise PipelineError("visual has an incomplete source ledger entry")
    return entry
