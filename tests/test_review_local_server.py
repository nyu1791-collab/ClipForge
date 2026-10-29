"""Check the local review prototype's data and loopback boundaries."""

import json
import re
import selectors
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import zipfile

import pytest

from clipforge import review_local_server
from clipforge.storage import PipelineError, fingerprint


def review_archive(path, *, alter_editor_data=False):
    selection = {
        "selection_basis": "audio_understanding",
        "source_url": "https://example.test/video.mp4",
        "clips": [{"start": 0, "end": 5}],
    }
    transcripts = {
        "clips": [{"segments": [{"start": 1, "end": 2, "text": "元の字幕"}]}]
    }
    editor_data = {
        "fingerprint": fingerprint(
            {"selection": selection, "transcripts": transcripts}
        ),
        "clips": [{"duration": 5, "segments": transcripts["clips"][0]["segments"]}],
    }
    if alter_editor_data:
        editor_data["clips"][0]["segments"] = [
            {"start": 1, "end": 2, "text": "別の字幕"}
        ]
    page = (
        '<button onclick="saveEdits()">修正ファイルを保存</button>'
        f'<script type="application/json" id="editor-data">{json.dumps(editor_data)}</script>'
        "<script></script></html>"
    )
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("review.html", page)
        bundle.writestr("final-selection.json", json.dumps(selection))
        bundle.writestr("selected-transcripts.json", json.dumps(transcripts))
        bundle.writestr("clip-001.mp4", b"0123456789")
    return editor_data["fingerprint"]


def test_review_page_payload_must_match_original_subtitles(tmp_path):
    package = tmp_path / "review.zip"
    review_archive(package, alter_editor_data=True)
    with pytest.raises(PipelineError, match="does not match"):
        review_local_server.prepare(package)


def test_review_server_streams_and_saves_only_valid_same_origin_patches(tmp_path):
    package = tmp_path / "review.zip"
    original_fingerprint = review_archive(package)
    output = tmp_path / "private" / "patch.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "clipforge.review_local_server",
            str(package),
            "--patch-output",
            str(output),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "local server did not start"
        line = process.stdout.readline()
        match = re.search(r"http://127\.0\.0\.1:\d+", line)
        assert match, line
        base = match.group()
        opener = build_opener(ProxyHandler({}))
        with opener.open(base + "/") as response:
            page = response.read().decode()
        token = json.loads(re.search(r"const localToken = (.*?);", page).group(1))
        with opener.open(
            Request(base + "/clip-001.mp4", headers={"Range": "bytes=2-5"})
        ) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 2-5/10"
            assert response.read() == b"2345"

        patch = {
            "reason": "利用者による字幕修正",
            "input_fingerprint": original_fingerprint,
            "clips": [
                {
                    "clip": 1,
                    "segments": [{"segment": 1, "start": 1, "end": 2, "text": "修正"}],
                }
            ],
        }

        def save(value, origin):
            return opener.open(
                Request(
                    base + "/patch",
                    data=json.dumps(value).encode(),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": origin,
                        "X-Review-Token": token,
                    },
                )
            )

        with pytest.raises(HTTPError) as rejected_origin:
            save(patch, "https://other.example")
        assert rejected_origin.value.code == 403
        assert not output.exists()
        with save(patch, base) as response:
            assert response.status == 204
        assert json.loads(output.read_text()) == patch
        with opener.open(base + "/patch") as response:
            assert json.loads(response.read()) == patch
        wrong_parent = {**patch, "input_fingerprint": "wrong"}
        with pytest.raises(HTTPError) as rejected_parent:
            save(wrong_parent, base)
        assert rejected_parent.value.code == 400
        assert json.loads(output.read_text()) == patch
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)
