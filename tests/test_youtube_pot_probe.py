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
                    data=json.dumps({"poToken": token}).encode(),
                    url="http://127.0.0.1:4416/get_pot",
                )
            )
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


def test_audio_probe_preserves_canonical_failure_receipt(tmp_path, monkeypatch):
    import sys
    from clipforge.storage import PipelineError

    repository = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "probe_audio_failure", repository / "tools/probe_youtube_audio.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def failed(arguments):
        directory = Path(arguments[arguments.index("--directory") + 1])
        directory.mkdir()
        (directory / "acquisition-report.json").write_text(
            json.dumps(
                {
                    "status": "acquisition_failed",
                    "phase": "acquire",
                    "error_type": "PipelineError",
                }
            )
        )
        raise PipelineError("provider denied metadata")

    monkeypatch.setitem(sys.modules, "acquire_audio_url", SimpleNamespace(main=failed))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUEST_ID", "a" * 32)
    monkeypatch.setenv("SOURCE_REF", "b" * 40)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe_youtube_audio.py",
            "--url",
            "https://youtu.be/6ras3m-gFuc",
            "--directory",
            str(tmp_path / "audio"),
        ],
    )
    module.main()
    assert (
        json.loads((tmp_path / "probe-result/audio.json").read_text())["status"]
        == "audio_failed"
    )
    assert (
        json.loads((tmp_path / "probe-result/canonical-acquisition.json").read_text())[
            "phase"
        ]
        == "acquire"
    )
