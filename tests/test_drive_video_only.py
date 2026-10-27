"""Drive delivery accepts only finished videos, with all services mocked."""

import importlib
import json
from pathlib import Path
import socket


def test_delivery_uploads_only_rendered_mp4s_without_subtitles(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    delivery = importlib.import_module("clipforge_release_delivery")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("RENDER_RUN", "123")
    monkeypatch.setenv("RENDER_REQUEST", "a" * 32)
    monkeypatch.setenv("SOURCE_REF", "b" * 40)

    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(
        delivery, "read_transfer", lambda *args: {"source_ref": "b" * 40}
    )
    monkeypatch.setattr(delivery, "authorize_target", lambda: {"synthetic": "auth"})
    paths = [f"clips/clip-{i:03}" for i in range(1, 5)]
    hashes = {f"{p}/video.mp4": "f" * 64 for p in paths}

    def download(run, name, root):
        root.mkdir()
        (root / "manifest.json").write_text(
            json.dumps(
                {"status": "complete", "rendered": [{"directory": p} for p in paths]}
            )
        )
        for path in paths:
            directory = root / path
            directory.mkdir(parents=True)
            (directory / "video.mp4").write_bytes(b"synthetic video")
            (directory / "captions.srt").write_text("private subtitle")

    monkeypatch.setattr(delivery, "download", download)
    monkeypatch.setattr(
        delivery,
        "validate_identity",
        lambda *args, **kwargs: {"status": "complete", "sha256": hashes},
    )
    uploads = []

    class Response:
        def __init__(self, value, headers=None):
            self.value = json.dumps(value).encode()
            self.headers = headers or {}

        def read(self):
            return self.value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def request(url, method="GET", payload=None, headers=None, raw=None):
        if method == "POST" and "uploadType=resumable" in url:
            uploads.append(payload["name"])
            assert headers["X-Upload-Content-Type"] == "video/mp4"
            return Response(
                {}, {"Location": "https://www.googleapis.com/upload/synthetic"}
            )
        if method == "POST":
            return Response(
                {"id": "folder", "webViewLink": "https://drive.example/folder"}
            )
        assert method == "PUT" and raw == b"synthetic video"
        return Response(
            {
                "name": uploads[-1],
                "size": len(raw),
                "webViewLink": "https://drive.example/file",
            }
        )

    monkeypatch.setattr(delivery, "request", request)
    delivery.deliver()
    assert uploads == [f"clip-{i:03}.mp4" for i in range(1, 5)]
    assert not (tmp_path / "ClipForge-records.zip").exists()
