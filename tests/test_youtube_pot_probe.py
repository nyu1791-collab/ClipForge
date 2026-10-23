import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location(
        "probe_youtube_pot",
        Path(__file__).resolve().parents[1] / "tools/probe_youtube_pot.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_records_generation_and_attachment_without_exposing_tokens(
    probe, monkeypatch, capsys
):
    import yt_dlp
    from yt_dlp.utils import DownloadError

    token = "private-test-pot-must-not-appear"
    options_seen = []

    class FakeDownloader:
        def __init__(self, options):
            self.options = options
            options_seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def urlopen(self, request):
            return None

        def extract_info(self, url, download):
            assert download is False
            logger = self.options["logger"]
            for line in [
                "PO Token Providers: bgutil:http-2.0.2",
                "Generating a player PO Token",
                "Generated POT: " + token,
            ]:
                logger.debug(line)
            self.urlopen(
                SimpleNamespace(
                    data=json.dumps(
                        {"serviceIntegrityDimensions": {"poToken": token}}
                    ).encode(),
                    url="https://youtube.com/youtubei/v1/player",
                )
            )
            raise DownloadError("Sign in to confirm you are not a bot " + token)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeDownloader)
    probe.worker("https://youtu.be/abcdefghijk", "always")
    output = capsys.readouterr()
    assert token not in output.out + output.err
    report = json.loads(output.out)
    assert report["status"] == "metadata_failed"
    assert report["token_generated"] is True
    assert report["player_requests_with_token"] == 1
    assert report["generation_attempted"] is True
    assert "cookiefile" not in options_seen[0]
    assert options_seen[0]["extractor_args"]["youtube"]["fetch_pot"] == ["always"]
