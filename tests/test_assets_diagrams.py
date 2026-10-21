import io
import socket
import subprocess
import urllib.error
import urllib.request

import pytest
from PIL import Image

from clipforge import assets, diagrams, thumbnail
from clipforge.models import Diagram
from clipforge.storage import PipelineError


def png_bytes(size=(64, 48), color=(20, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_check_image_accepts_only_jpeg_png_and_webp(tmp_path):
    good = tmp_path / "good.png"
    good.write_bytes(png_bytes())
    assert assets.check_image(good) == "PNG"
    gif = tmp_path / "anim.gif"
    Image.new("RGB", (8, 8)).save(gif, format="GIF")
    with pytest.raises(PipelineError, match="unsupported"):
        assets.check_image(gif)
    text = tmp_path / "text.png"
    text.write_text("not an image")
    with pytest.raises(PipelineError, match="not a valid image"):
        assets.check_image(text)


def test_ledger_entry_needs_source_credit_and_licence():
    complete = {
        "file": "a.png",
        "sha256": "0" * 64,
        "source_url": "https://example.org/a",
        "source_credit": "Author / CC0",
        "license": "CC0",
    }
    assert assets.validate_entry(complete) is complete
    for key in ("source_credit", "license", "source_url"):
        with pytest.raises(PipelineError, match="incomplete"):
            assets.validate_entry({**complete, key: ""})


def test_public_https_refuses_plain_http_and_private_addresses():
    with pytest.raises(PipelineError, match="only https"):
        assets._public_https("http://upload.wikimedia.org/a.jpg")
    with pytest.raises(PipelineError, match="non-public"):
        assets._public_https("https://127.0.0.1/a.jpg")
    with pytest.raises(PipelineError, match="non-public"):
        assets._public_https("https://localhost/a.jpg")


def test_public_https_accepts_a_public_address_and_reports_resolution_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 443))],
    )
    assert (
        assets._public_https("https://example.org/a.jpg") == "https://example.org/a.jpg"
    )

    def unresolvable(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", unresolvable)
    with pytest.raises(PipelineError, match="could not be resolved"):
        assets._public_https("https://example.org/a.jpg")


def test_every_redirect_hop_is_checked():
    handler = assets._PublicRedirects()
    request = urllib.request.Request("https://upload.wikimedia.org/x")
    for target in ["http://upload.wikimedia.org/a.jpg", "https://127.0.0.1/a.jpg"]:
        with pytest.raises(PipelineError):
            handler.redirect_request(request, None, 302, "Found", {}, target)


def test_retry_after_is_bounded_and_defaults_to_five_seconds():
    assert assets._retry_after({"Retry-After": "7"}) == 7
    assert assets._retry_after({"Retry-After": "999"}) == 30
    assert assets._retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) == 5
    assert assets._retry_after({}) == 5


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self.data[:size]


def test_get_retries_once_on_rate_limit_then_returns_the_body(monkeypatch):
    replies = [
        urllib.error.HTTPError(
            "https://example.org", 429, "slow down", {"Retry-After": "1"}, None
        ),
        FakeResponse(b"payload"),
    ]
    slept = []

    class Opener:
        def open(self, request, timeout):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr(assets, "_OPENER", Opener())
    monkeypatch.setattr(assets, "_public_https", lambda url: url)
    monkeypatch.setattr(assets.time, "sleep", slept.append)
    assert assets._get("https://example.org/x") == b"payload"
    assert slept == [1]


def test_get_reports_http_errors_transport_failures_and_oversize_bodies(monkeypatch):
    class Opener:
        def __init__(self, reply):
            self.reply = reply

        def open(self, request, timeout):
            if isinstance(self.reply, Exception):
                raise self.reply
            return FakeResponse(self.reply)

    monkeypatch.setattr(assets, "_public_https", lambda url: url)
    cases = [
        (urllib.error.HTTPError("u", 500, "err", {}, None), "HTTP 500"),
        (urllib.error.URLError("refused"), "request failed"),
        (TimeoutError("slow"), "request failed"),
        (b"x" * (assets.MAX_BYTES + 1), "larger than"),
    ]
    for reply, message in cases:
        monkeypatch.setattr(assets, "_OPENER", Opener(reply))
        with pytest.raises(PipelineError, match=message):
            assets._get("https://example.org/x")


def test_json_replies_that_are_not_json_are_refused(monkeypatch):
    monkeypatch.setattr(assets, "_get", lambda url: b"<html>blocked</html>")
    with pytest.raises(PipelineError, match="unreadable"):
        assets._json("https://example.org/api")


def test_commons_results_keep_only_reusable_licences(monkeypatch):
    payload = {
        "query": {
            "pages": [
                {
                    "imageinfo": [
                        {
                            "thumburl": "https://upload.wikimedia.org/a.jpg",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/File:A.jpg",
                            "mime": "image/jpeg",
                            "extmetadata": {
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "Artist": {"value": "<a>Photographer</a>"},
                                "LicenseUrl": {
                                    "value": "https://creativecommons.org/licenses/by-sa/4.0"
                                },
                            },
                        }
                    ]
                },
                {
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/b.jpg",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/File:B.jpg",
                            "mime": "image/jpeg",
                            "extmetadata": {"LicenseShortName": {"value": "GFDL"}},
                        }
                    ]
                },
                {
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/c.svg",
                            "mime": "image/svg+xml",
                            "extmetadata": {"LicenseShortName": {"value": "CC0"}},
                        }
                    ]
                },
            ]
        }
    }
    monkeypatch.setattr(assets, "_json", lambda url: payload)
    results = assets.search_commons("富士山", 5)
    assert len(results) == 1
    assert results[0]["license"] == "CC BY-SA 4.0"
    assert (
        results[0]["source_credit"] == "Photographer / CC BY-SA 4.0 / Wikimedia Commons"
    )
    assert results[0]["provider"] == "wikimedia-commons"
    monkeypatch.setattr(assets, "_json", lambda url: {})
    assert assets.search_commons("none", 5) == []


def test_openverse_results_name_their_licence(monkeypatch):
    payload = {
        "results": [
            {"license": "cc0", "url": "https://img/a.jpg", "creator": "A"},
            {"license": "pdm", "url": "https://img/b.jpg", "creator": ""},
            {
                "license": "by-sa",
                "license_version": "4.0",
                "url": "https://img/c.jpg",
                "foreign_landing_url": "https://site/c",
                "creator": "C",
            },
            {"license": "by-nc", "url": "https://img/d.jpg"},
            {"license": "cc0"},
        ]
    }
    monkeypatch.setattr(assets, "_json", lambda url: payload)
    results = assets.search_openverse("fuji", 10)
    assert [r["license"] for r in results] == ["CC0", "Public domain", "CC BY-SA 4.0"]
    assert results[1]["source_credit"] == "unknown author / Public domain / Openverse"
    assert results[2]["source_url"] == "https://site/c"


def test_one_failing_provider_does_not_stop_the_others(monkeypatch):
    def broken(query, limit):
        raise PipelineError("down")

    monkeypatch.setattr(assets, "search_commons", broken)
    monkeypatch.setattr(
        assets,
        "search_openverse",
        lambda query, limit: [{"provider": "openverse", "query": query}],
    )
    assert assets.search("fuji", ["commons", "openverse", "unknown"], 2) == [
        {"provider": "openverse", "query": "fuji"}
    ]


def test_downloads_are_validated_saved_and_recorded(monkeypatch, tmp_path):
    items = [
        {
            "image_url": "https://img/1",
            "provider": "wikimedia-commons",
            "source_url": "u1",
            "source_credit": "A / CC0 / Wikimedia Commons",
            "license": "CC0",
            "license_url": "",
            "query": "q",
        },
        {
            "image_url": "https://img/2",
            "provider": "openverse",
            "source_url": "u2",
            "source_credit": "B / CC0 / Openverse",
            "license": "CC0",
            "license_url": "",
            "query": "q",
        },
        {
            "image_url": "https://img/3",
            "provider": "openverse",
            "source_url": "u3",
            "source_credit": "C",
            "license": "CC0",
            "license_url": "",
            "query": "q",
        },
    ]
    payloads = {
        "https://img/1": png_bytes(),
        "https://img/2": b"<html>",
        "https://img/3": png_bytes(),
    }

    def fetch(url):
        return payloads[url]

    monkeypatch.setattr(assets, "_get", fetch)
    entries = assets.download_all(items, tmp_path / "visuals")
    assert [e["file"] for e in entries] == ["image-01.png", "image-03.png"]
    for entry in entries:
        assets.validate_entry(entry)
        assert (tmp_path / "visuals" / entry["file"]).is_file()
    assert not list((tmp_path / "visuals").glob("*.tmp"))
    assert entries[0]["kind"] == "photo" and entries[0]["license"] == "CC0"


def test_oversize_downloads_are_refused_and_leave_no_partial_file(
    monkeypatch, tmp_path
):
    class Opener:
        def open(self, request, timeout):
            return FakeResponse(b"\0" * (assets.MAX_BYTES + 1))

    monkeypatch.setattr(assets, "_OPENER", Opener())
    monkeypatch.setattr(assets, "_public_https", lambda url: url)
    item = {
        "image_url": "https://img/huge.png",
        "provider": "p",
        "source_url": "s",
        "source_credit": "c",
        "license": "CC0",
        "license_url": "",
        "query": "q",
    }
    with pytest.raises(PipelineError, match="larger"):
        assets._download_one(item, tmp_path, 1)
    assert list(tmp_path.iterdir()) == []


def test_svg_with_external_reference_is_refused(tmp_path):
    path = tmp_path / "bad.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>'
    )
    with pytest.raises(PipelineError, match="external"):
        diagrams.sanitized_svg(path)


@pytest.mark.parametrize(
    "markup",
    [
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg/>',
        '<svg><svg:image xmlns:svg="http://www.w3.org/2000/svg"/></svg>',
        '<svg><rect fill="url(#g)"/></svg>',
        '<svg><style>@import "x.css";</style></svg>',
        "<svg><script>alert(1)</script></svg>",
    ],
)
def test_svg_refuses_dtds_prefixed_elements_and_remote_styles(tmp_path, markup):
    path = tmp_path / "x.svg"
    path.write_text(markup)
    with pytest.raises(PipelineError):
        diagrams.sanitized_svg(path)


def test_svg_size_limit_and_clean_rasterising(tmp_path):
    big = tmp_path / "big.svg"
    big.write_text("<svg>" + " " * (2 * 1024 * 1024) + "</svg>")
    with pytest.raises(PipelineError, match="larger"):
        diagrams.sanitized_svg(big)
    clean = tmp_path / "clean.svg"
    clean.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect width="40" height="20" fill="#1e88e5"/></svg>'
    )
    output = tmp_path / "clean.png"
    diagrams.svg_to_png(clean, output)
    assert Image.open(output).size[0] == 1280


def test_each_diagram_kind_renders_a_fixed_width_png(japanese_font, tmp_path):
    specs = [
        Diagram(
            kind="table",
            title="比較",
            start=0,
            end=1,
            rows=[["項目", "値"], ["A", "1"]],
        ),
        Diagram(
            kind="bars",
            title="推移",
            start=0,
            end=1,
            labels=["1月", "2月"],
            values=[3, 5.5],
        ),
        Diagram(kind="steps", title="手順", start=0, end=1, steps=["準備", "実行"]),
    ]
    for index, spec in enumerate(specs):
        target = tmp_path / f"d{index}.png"
        diagrams.render(spec, japanese_font, target)
        assert Image.open(target).size[0] == diagrams.WIDTH


def test_long_titles_shrink_to_fit_the_diagram(japanese_font, tmp_path):
    target = tmp_path / "long.png"
    diagrams.table("長い" * 40, [["a", "b"]], japanese_font, target)
    assert Image.open(target).size[0] == diagrams.WIDTH


def test_thumbnail_is_a_cover_fitted_frame_with_the_title(
    monkeypatch, tmp_path, japanese_font
):
    def fake_ffmpeg(command, **kwargs):
        Image.new("RGB", (640, 360), (40, 80, 120)).save(command[-1], format="PNG")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(thumbnail.subprocess, "run", fake_ffmpeg)
    destination = tmp_path / "thumbnail.png"
    thumbnail.make(
        tmp_path / "video.mp4",
        destination,
        "とても長いタイトルの切り抜き動画です",
        japanese_font,
        3.0,
    )
    assert Image.open(destination).size == thumbnail.SIZE
    assert not (tmp_path / "thumbnail.frame.png").exists()


def test_thumbnail_reports_a_failed_frame_extraction(
    monkeypatch, tmp_path, japanese_font
):
    monkeypatch.setattr(
        thumbnail.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "x"),
    )
    with pytest.raises(PipelineError, match="could not be extracted"):
        thumbnail.make(
            tmp_path / "video.mp4", tmp_path / "t.png", "題", japanese_font, 0
        )
