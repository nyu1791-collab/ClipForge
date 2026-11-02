"""The real audio-route render with FFmpeg; only the source fetch and NVENC are replaced."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from clipforge import audio_first, audio_understanding, media, pipeline
from clipforge.models import Settings
from clipforge.sound_effects import RECIPES
from clipforge.worker_audio import supports_audio_stage

CAPCUT = "config.tiktok-capcut.example.json"
CAPCUT_360P = "config.tiktok-capcut-360p.example.json"
TITLE = "見出しの確認"
LINE = "字幕の確認です"


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=blue:s=640x360:r=10:d=3", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3", "-shortest", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture
def run_route(tmp_path, source, monkeypatch, japanese_font):
    class Source:
        url = "https://example.test/video.mp4"

        def close(self):
            pass

    def retrieved(vod, target, start, end, quality):
        shutil.copyfile(source, target)
        return {"start_offset": 0.0}

    monkeypatch.setattr(audio_understanding, "open_source", lambda *a, **k: Source())
    monkeypatch.setattr(audio_first, "retrieved", retrieved)
    # This machine may have no NVENC session; the route forces the encoder on purpose.
    monkeypatch.setattr(
        media,
        "final_video_options",
        lambda settings: ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                          "-threads", "1", "-pix_fmt", "yuv420p"],
    )  # fmt: skip
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "source_url": Source.url,
                "selection_basis": "audio_understanding",
                "clips": [
                    {
                        "start": 0,
                        "end": 3,
                        "candidate_id": "one",
                        "title": TITLE,
                        "reason": "test",
                        "confidence": 1,
                        "speaker_turns": [],
                        "sound_effects": [
                            {"at": 1.0, "kind": next(iter(RECIPES)), "reason": "test"}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    transcripts = tmp_path / "transcripts.json"
    transcripts.write_text(
        json.dumps(
            {
                "subtitle_policy": audio_understanding.SUBTITLE_POLICY,
                "clips": [
                    {
                        "language": "ja",
                        "segments": [{"start": 0.2, "end": 2.8, "text": LINE}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def run(config, caption=None, name="out"):
        values = json.loads(Path(config).read_text())
        values["min_seconds"] = 2
        if caption:
            values["caption"] = caption
        output = tmp_path / name
        audio_understanding.render(
            review, transcripts, output, Settings.model_validate(values)
        )
        return output / "clips" / "clip-001"

    return run


def text_spread(video, top_share, bottom_share):
    """Brightness range in a horizontal band of one frame; flat when nothing is drawn."""
    info = media.probe(video)
    width, height = info["width"], info["height"]
    frame = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", "1.5", "-i", str(video),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True,
        capture_output=True,
    ).stdout  # fmt: skip
    band = frame[int(height * top_share) * width : int(height * bottom_share) * width]
    return max(band) - min(band)


HEADING_BAND = (0.0, 0.3)
CAPTION_BAND = (0.6, 0.98)


@pytest.mark.parametrize(
    ("config", "size"), [(CAPCUT, (720, 1280)), (CAPCUT_360P, (360, 640))]
)
def test_editable_route_keeps_the_video_clean_and_saves_subtitles(
    run_route, config, size
):
    clip = run_route(config)
    for name in ("video.mp4", "captions.srt", "captions.ass", "render.json"):
        assert (clip / name).is_file()
    receipt = json.loads((clip / "render.json").read_text(encoding="utf-8"))
    assert receipt["captions"]["caption_mode"] == "editable"
    assert (receipt["media"]["width"], receipt["media"]["height"]) == size
    # Measured without text: at most 19 from the encoded blur; burned text exceeds 60.
    assert text_spread(clip / "video.mp4", *HEADING_BAND) < 30
    assert text_spread(clip / "video.mp4", *CAPTION_BAND) < 30
    srt = (clip / "captions.srt").read_text(encoding="utf-8")
    ass = (clip / "captions.ass").read_text(encoding="utf-8")
    assert LINE in srt
    # The heading stays editable in the ASS only: it is neither burned nor spoken dialogue.
    assert TITLE not in srt and TITLE in ass and receipt["captions"]["heading"] is True
    # Sound effects are mixed into the audio, which the CapCut draft keeps.
    assert receipt["sound_effects"]["status"] == "synthesized_and_mixed"


@pytest.mark.parametrize("caption", ["plain", "karaoke"])
def test_plain_and_karaoke_still_burn_heading_and_captions(
    run_route, monkeypatch, caption
):
    passed = []
    original = media.render

    def spy(source, destination, clip, settings, **kwargs):
        passed.append((settings.caption, kwargs.get("ass")))
        return original(source, destination, clip, settings, **kwargs)

    monkeypatch.setattr(pipeline.media, "render", spy)
    clip = run_route(CAPCUT_360P, caption=caption)
    # The saved-subtitle policy has always rendered karaoke configs as plain phrases.
    assert passed[0][0] == "plain"
    assert Path(passed[0][1]).name == "captions.ass"
    receipt = json.loads((clip / "render.json").read_text(encoding="utf-8"))
    assert receipt["captions"]["caption_mode"] == "plain"
    assert text_spread(clip / "video.mp4", *HEADING_BAND) > 60
    assert text_spread(clip / "video.mp4", *CAPTION_BAND) > 60


def test_360p_config_differs_from_capcut_only_in_height():
    default = json.loads(Path(CAPCUT).read_text())
    small = json.loads(Path(CAPCUT_360P).read_text())
    assert default.pop("height") == 1280 and small.pop("height") == 640
    assert default == small


def test_vertical_360p_size_rules():
    assert media.frame_size(Settings(aspect="9:16", height=640)) == (360, 640)
    # Height is the frame height: 9:16 at 360 is the narrow 202x360, not 360x640.
    assert media.frame_size(Settings(aspect="9:16", height=360)) == (202, 360)
    assert supports_audio_stage(Settings(aspect="9:16", height=640))
    assert not supports_audio_stage(Settings(aspect="9:16", height=360))
    for aspect in ("16:9", "1:1"):
        with pytest.raises(ValueError, match="height 640"):
            Settings(aspect=aspect, height=640)
