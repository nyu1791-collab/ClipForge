import pytest

from clipforge.models import Segment, Settings, Transcript, Word


@pytest.fixture
def settings():
    return Settings(
        clips=2,
        candidates=4,
        min_seconds=3,
        max_seconds=10,
    )


@pytest.fixture
def transcript():
    return Transcript(
        language="ja",
        segments=[
            Segment(
                start=1,
                end=4,
                text="最初の場面です。",
                words=[
                    Word(start=1, end=2, text="最初の"),
                    Word(start=2, end=4, text="場面です。"),
                ],
            ),
            Segment(
                start=8,
                end=13,
                text="これは重要な場面です。",
                words=[
                    Word(start=8, end=10, text="これは"),
                    Word(start=10, end=13, text="重要な場面です。"),
                ],
            ),
            Segment(start=22, end=27, text="最後まで確認します。", words=[]),
        ],
    )


@pytest.fixture
def on_target_loudness():
    """Compatibility fixture: production no longer performs a media scan."""
    return None


@pytest.fixture
def japanese_font():
    """Path of an installed Japanese font; the caption checks need one, so skip without it."""
    from clipforge import fonts
    from clipforge.storage import PipelineError

    for family in ("Noto Sans CJK JP", "IPAGothic"):
        try:
            return fonts.resolve(family)
        except PipelineError:
            continue
    pytest.skip("no Japanese caption font is installed")


@pytest.fixture(autouse=True)
def _no_gpu_encoder(monkeypatch):
    # The hardware probe runs FFmpeg; tests fake FFmpeg, so a fake success must not pick NVENC.
    from clipforge import hardware

    monkeypatch.setattr(hardware, "nvenc_available", lambda: False)
