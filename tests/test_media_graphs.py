import shutil
import subprocess
from types import SimpleNamespace

import pytest

from clipforge import hardware, media
from clipforge.models import SelectedClip, Settings
from clipforge.storage import PipelineError

CLIP = SimpleNamespace(start=10.0, end=20.0)


def make(**overrides):
    return Settings(
        **{
            "min_seconds": 3,
            "max_seconds": 30,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    ("aspect", "requested", "width", "height"),
    [
        ("9:16", 720, 404, 720),
        ("16:9", 720, 1280, 720),
        ("1:1", 720, 720, 720),
        ("9:16", 1080, 608, 1080),
        ("9:16", None, 1080, 1920),
        ("16:9", None, 1920, 1080),
        ("1:1", None, 1080, 1080),
    ],
)
def test_frame_size_follows_the_aspect_ratio(aspect, requested, width, height):
    assert media.frame_size(make(aspect=aspect, height=requested)) == (width, height)


def test_default_height_is_the_standard_size_for_each_aspect():
    assert media.frame_size(make()) == (1080, 1920)
    assert media.frame_size(make(aspect="16:9")) == (1920, 1080)
    with pytest.raises(ValueError, match="only for the 9:16"):
        make(aspect="16:9", height=1920)


def test_layouts_end_with_the_requested_label():
    fit = media._layout(make(framing="fit", layout="fit"), 404, 720, "vin", "out")
    assert "pad=404:720" in fit and fit.endswith("[out]")
    crop = media._layout(
        make(framing="center-crop", layout="fit"), 404, 720, "vin", "out"
    )
    assert "crop=404:720" in crop and "pad=" not in crop
    blur = media._layout(make(layout="blur"), 404, 720, "vin", "out")
    assert (
        "boxblur=8:2" in blur and "split=2[bgs][fgs]" in blur and blur.endswith("[out]")
    )


def test_video_graph_chains_fade_progress_and_captions():
    settings = make(progress_bar=True, fade_seconds=0.5)
    graph = media._video_graph(CLIP, settings, 404, 720, 10.0, "captions.ass")
    assert "fade=t=in:st=0:d=0.500" in graph
    assert "fade=t=out:st=9.500:d=0.500" in graph
    assert "color=c=black@0:s=404x8" in graph
    assert "overlay=0:712:shortest=1[pb]" in graph
    assert graph.endswith("[pb]ass=filename=captions.ass[vout]")


def test_plain_video_graph_has_no_optional_stages():
    settings = make(progress_bar=False, fade_seconds=0, layout="fit")
    graph = media._video_graph(CLIP, settings, 404, 720, 10.0, None)
    assert "split=" not in graph and "fade=" not in graph and "geq=" not in graph
    assert graph.endswith("null[vout]")


def test_audio_graph_is_the_narration_on_the_clip_clock():
    graph = media._audio_graph()
    assert graph == "[0:a:0]asetpts=PTS-STARTPTS[mix]"
    assert "amix" not in graph and "anullsrc" not in graph


def test_loudness_arguments_use_the_first_pass_measurement():
    assert media._loudness_args(None) == "loudnorm=I=-16:TP=-2.0:LRA=11"
    measured = {
        "input_i": -20.1,
        "input_tp": -3.2,
        "input_lra": 5.0,
        "input_thresh": -30.0,
        "target_offset": 0.4,
    }
    assert media._loudness_args(measured) == (
        "loudnorm=I=-16:TP=-2.0:LRA=11:measured_I=-20.10:measured_TP=-3.20:"
        "measured_LRA=5.00:measured_thresh=-30.00:offset=0.40:linear=true"
    )


def test_peak_limiter_follows_loudnorm_and_resample_and_precedes_the_fades():
    # 0.7079 is -3 dBFS as a linear amplitude.
    assert media.PEAK_LIMIT == pytest.approx(10 ** (-3 / 20), abs=1e-4)
    tail = media._audio_tail(make(fade_seconds=0.3), 10.0, None)
    limiter = f"alimiter=limit={media.PEAK_LIMIT}:level=disabled:attack=5:release=50"
    order = [
        tail.index("loudnorm="),
        tail.index(f",aresample={media.AUDIO_RATE},"),
        tail.index(limiter),
        tail.index("afade=t=in"),
        tail.index("[aout]"),
    ]
    assert order == sorted(order)
    assert limiter in media._audio_tail(make(fade_seconds=0), 10.0, None)


def test_audio_tail_fades_only_when_the_clip_is_long_enough():
    tail = media._audio_tail(make(fade_seconds=0.3), 10.0, None)
    assert "afade=t=in:st=0:d=0.300" in tail and "afade=t=out:st=9.700:d=0.300" in tail
    assert tail.endswith("[aout]")
    assert "afade" not in media._audio_tail(make(fade_seconds=0), 10.0, None)
    assert "afade" not in media._audio_tail(make(fade_seconds=0.3), 0.5, None)


def test_preview_removes_stale_output_and_reports_decoding_errors(
    monkeypatch, tmp_path
):
    candidate = SimpleNamespace(start=5.0, end=9.0)
    destination = tmp_path / "preview.mp4"
    destination.write_bytes(b"stale")
    monkeypatch.setattr(
        media,
        "process",
        lambda command, timeout, **kwargs: subprocess.CompletedProcess(
            command, 0, b"", b""
        ),
    )
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 4.0})
    assert media.preview("/in.mp4", destination, candidate, make()) == {"duration": 4.0}
    assert not destination.exists()
    monkeypatch.setattr(
        media,
        "process",
        lambda command, timeout, **kwargs: subprocess.CompletedProcess(
            command, 0, b"", b"Invalid data"
        ),
    )
    with pytest.raises(PipelineError, match="decoding error"):
        media.preview("/in.mp4", destination, candidate, make())


def test_audio_extraction_reports_decoding_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media,
        "process",
        lambda command, timeout, **kwargs: subprocess.CompletedProcess(
            command, 0, b"", b""
        ),
    )
    media.audio("/in.mp4", tmp_path / "a.wav", 10.0)
    monkeypatch.setattr(
        media,
        "process",
        lambda command, timeout, **kwargs: subprocess.CompletedProcess(
            command, 0, b"", b"bad packet"
        ),
    )
    with pytest.raises(PipelineError, match="decoding error"):
        media.audio("/in.mp4", tmp_path / "a.wav", 10.0)


def clip_for_render():
    return SelectedClip(
        start=10, end=20, candidate_id="manual", title="t", reason="r", confidence=1
    )


@pytest.fixture
def probed(monkeypatch):
    monkeypatch.setattr(
        media,
        "probe",
        lambda path: {"duration": 10, "width": 404, "height": 720, "audio": True},
    )


@pytest.mark.parametrize("explicit,expected", [(0, "8"), (6, "6")])
@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc"])
def test_render_passes_render_thread_budget_to_ffmpeg(
    monkeypatch, tmp_path, probed, explicit, expected, encoder
):
    seen = {}

    def fake(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(hardware.os, "sched_getaffinity", lambda pid: set(range(8)))
    monkeypatch.setattr(hardware, "nvenc_available", lambda: True)
    monkeypatch.setattr(media, "process", fake)
    media.render(
        "/in.mp4",
        tmp_path / "out.mp4",
        clip_for_render(),
        make(
            height=720,
            encode_threads=explicit,
            video_encoder=encoder,
            loudness_two_pass=False,
        ),
    )
    command = seen["command"]
    assert command.count("-threads") == 2  # input decoder and output encoder
    for i, option in enumerate(command):
        if option in {"-threads", "-filter_threads", "-filter_complex_threads"}:
            assert command[i + 1] == expected
    assert "-filter_threads" in command and "-filter_complex_threads" in command


def test_preview_passes_render_thread_budget_to_ffmpeg(monkeypatch, tmp_path, probed):
    seen = {}

    def fake(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(media, "process", fake)
    media.preview(
        "/in.mp4",
        tmp_path / "preview.mp4",
        CLIP,
        make(encode_threads=6, video_encoder="libx264"),
    )
    command = seen["command"]
    assert command.count("-threads") == 2
    assert "-filter_threads" in command
    for i, option in enumerate(command):
        if option in {"-threads", "-filter_threads"}:
            assert command[i + 1] == "6"


CHAT_PANEL = {
    "chat_panel": True,
    "aspect": "16:9",
    "height": 1080,
    "chat_panel_width": 480,
}


def test_video_graph_without_the_chat_panel_is_unchanged():
    settings = make(progress_bar=False, fade_seconds=0, layout="fit")
    assert media._video_graph(CLIP, settings, 404, 720, 10.0, "captions.ass") == (
        "[0:V:0]setpts=PTS-STARTPTS[vin];"
        "[vin]scale=404:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=404:720:(ow-iw)/2:(oh-ih)/2,setsar=1[lay0];"
        "[lay0]ass=filename=captions.ass[vout]"
    )


def test_chat_panel_crops_the_video_left_of_the_column_and_pads_the_right():
    settings = make(
        **CHAT_PANEL, chat_panel_fit="crop", progress_bar=False, fade_seconds=0
    )
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, 10.0, "captions.ass", "chat.ass"
    )
    assert (
        "[vin]scale=1440:1080:force_original_aspect_ratio=increase:"
        "force_divisible_by=2,crop=1440:1080,setsar=1[lay0]"
    ) in graph
    assert graph.endswith(
        "[lay0]pad=1920:1080:0:0:color=0x111114,"
        "drawbox=x=1440:y=0:w=2:h=1080:color=0x2a2a30:t=fill,"
        "ass=filename=captions.ass,ass=filename=chat.ass[vout]"
    )


def test_chat_panel_letterbox_shrinks_the_video_between_bands():
    settings = make(
        **CHAT_PANEL, chat_panel_fit="letterbox", progress_bar=False, fade_seconds=0
    )
    graph = media._video_graph(CLIP, settings, 1920, 1080, 10.0, None, "chat.ass")
    assert (
        "[vin]scale=1440:1080:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,pad=1440:1080:(ow-iw)/2:(oh-ih)/2:color=0x111114,setsar=1[lay0]"
    ) in graph
    assert graph.endswith(
        "[lay0]pad=1920:1080:0:0:color=0x111114,"
        "drawbox=x=1440:y=0:w=2:h=1080:color=0x2a2a30:t=fill,"
        "ass=filename=chat.ass[vout]"
    )


def test_chat_panel_progress_use_the_video_area():
    settings = make(**CHAT_PANEL, fade_seconds=0)
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, 10.0, "captions.ass", "chat.ass"
    )
    assert "color=c=black@0:s=1440x8:r=30:d=10.000000" in graph
    assert "overlay=0:1072:shortest=1[pb]" in graph
    assert "ass=filename=captions.ass,ass=filename=chat.ass" in graph


TOP = {**CHAT_PANEL, "chat_panel_fit": "top"}


def test_chat_panel_top_puts_the_picture_above_a_caption_band():
    settings = make(**TOP, progress_bar=False, fade_seconds=0)
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, 10.0, "captions.ass", "chat.ass"
    )
    # The 16:9 picture is 1440x810 at the top left; the band under it has the column colour.
    assert (
        "[vin]scale=1440:810:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,pad=1440:810:(ow-iw)/2:(oh-ih)/2:color=0x111114,setsar=1[lay0]"
    ) in graph
    assert graph.endswith(
        "[lay0]pad=1920:1080:0:0:color=0x111114,"
        "drawbox=x=1440:y=0:w=2:h=1080:color=0x2a2a30:t=fill,"
        "drawbox=x=0:y=810:w=1440:h=2:color=0x2a2a30:t=fill,"
        "ass=filename=captions.ass,ass=filename=chat.ass[vout]"
    )


def test_chat_panel_top_progress_use_the_picture():
    settings = make(**TOP, fade_seconds=0)
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, 10.0, "captions.ass", "chat.ass"
    )
    assert "color=c=black@0:s=1440x8:r=30:d=10.000000" in graph
    assert "overlay=0:802:shortest=1[pb]" in graph
    assert "ass=filename=captions.ass,ass=filename=chat.ass" in graph


def test_chat_panel_top_scales_the_picture_with_the_column_width():
    settings = make(**{**TOP, "chat_panel_width": 400}, fade_seconds=0)
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, 10.0, "captions.ass", "chat.ass"
    )
    # The video area is 1520 px wide; its 16:9 picture is 856 px high (855 rounded to even).
    assert "[vin]scale=1520:856:" in graph
    assert "pad=1920:1080:0:0:color=0x111114,drawbox=x=1520:y=0" in graph
    assert "drawbox=x=1520:y=0:w=2:h=1080" in graph
    assert "drawbox=x=0:y=856:w=1520:h=2" in graph
    assert "color=c=black@0:s=1520x8:r=30" in graph
    assert "overlay=0:848:shortest=1[pb]" in graph


def test_chat_ass_is_refused_without_the_setting_or_outside_its_name(tmp_path):
    with pytest.raises(PipelineError, match="chat panel setting"):
        media.render(
            "/in.mp4",
            tmp_path / "out.mp4",
            clip_for_render(),
            make(),
            chat_ass=tmp_path / "chat.ass",
        )
    settings = make(**CHAT_PANEL)
    with pytest.raises(PipelineError, match="chat.ass beside"):
        media.render(
            "/in.mp4",
            tmp_path / "out.mp4",
            clip_for_render(),
            settings,
            chat_ass=tmp_path / "other.ass",
        )
    with pytest.raises(PipelineError, match="chat file is missing"):
        media.render(
            "/in.mp4",
            tmp_path / "out.mp4",
            clip_for_render(),
            settings,
            chat_ass=tmp_path / "chat.ass",
        )


@pytest.mark.parametrize("fit", ["crop", "letterbox", "top"])
def test_chat_panel_renders_a_real_landscape_frame(tmp_path, fit):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x480:rate=30:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    destination = tmp_path / "video.mp4"
    chat = tmp_path / "chat.ass"
    chat.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, Alignment\n"
        "Style: Default,Noto Sans CJK JP,40,&H00FFFFFF,7\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:02.00,Default,{\\pos(1500,200)}テスト\n",
        encoding="utf-8",
    )
    settings = make(
        **CHAT_PANEL,
        chat_panel_fit=fit,
        loudness_two_pass=False,
        progress_bar=False,
        fade_seconds=0,
    )
    clip = SimpleNamespace(start=0.0, end=3.0)
    info = media.render(source, destination, clip, settings, chat_ass=chat)
    assert (info["width"], info["height"]) == (1920, 1080)


FILL = {**CHAT_PANEL, "chat_panel_fit": "fill"}


def test_chat_panel_fill_scales_the_picture_to_the_area_and_centres_the_crop():
    settings = make(**FILL, progress_bar=False, fade_seconds=0)
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, 10.0, "captions.ass", "chat.ass"
    )
    # The picture is scaled to the frame height and cut to the 1440 px area, centred.
    assert (
        "[vin]scale=1440:1080:force_original_aspect_ratio=increase:"
        "force_divisible_by=2,crop=1440:1080:(iw-1440)/2:(ih-1080)/2,setsar=1[lay0]"
    ) in graph
    assert graph.endswith(
        "[lay0]pad=1920:1080:0:0:color=0x111114,"
        "drawbox=x=1440:y=0:w=2:h=1080:color=0x2a2a30:t=fill,"
        "ass=filename=captions.ass,ass=filename=chat.ass[vout]"
    )
    # The picture has the full frame height, so the only border is the vertical column line.
    assert graph.count("drawbox") == 1


def test_chat_panel_fill_crops_from_the_planned_x_fraction():
    settings = make(**FILL, progress_bar=False, fade_seconds=0)
    graph = media._video_graph(
        CLIP,
        settings,
        1920,
        1080,
        10.0,
        "captions.ass",
        "chat.ass",
        crop_x=0.1,
    )
    assert "crop=1440:1080:iw*0.100000:(ih-1080)/2,setsar=1[lay0]" in graph


def test_chat_panel_fill_accepts_the_whole_range_of_crop_positions():
    settings = make(**FILL, progress_bar=False, fade_seconds=0)
    for crop_x in (0.0, 0.25):
        media._video_graph(
            CLIP, settings, 1920, 1080, 10.0, None, "chat.ass", crop_x=crop_x
        )
    for crop_x in (-0.01, 0.26, 1.0):
        with pytest.raises(PipelineError, match="crop position is outside"):
            media._video_graph(
                CLIP,
                settings,
                1920,
                1080,
                10.0,
                None,
                "chat.ass",
                crop_x=crop_x,
            )


def test_crop_position_does_not_change_the_other_chat_fits():
    for fit in ("crop", "letterbox", "top"):
        settings = make(**{**CHAT_PANEL, "chat_panel_fit": fit}, progress_bar=False)
        plain = media._video_graph(
            CLIP, settings, 1920, 1080, 10.0, "captions.ass", "chat.ass"
        )
        moved = media._video_graph(
            CLIP,
            settings,
            1920,
            1080,
            10.0,
            "captions.ass",
            "chat.ass",
            crop_x=0.1,
        )
        assert moved == plain


def test_final_audio_limiter_leaves_headroom_after_aac_encoding(tmp_path):
    import shutil
    from clipforge import quality

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for the final AAC peak check")
    target = tmp_path / "peaks.m4a"
    signal = "aevalsrc='if(lt(mod(t,0.1),0.001),0.99,0)':s=48000:d=2"
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        signal,
        "-filter_complex",
        "[0:a]anull[mix];" + media._audio_tail(make(), 2, None),
        "-map",
        "[aout]",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(target),
    ]
    subprocess.run(command, capture_output=True, check=True, timeout=30)
    assert quality.inspect_output(target, True, 10)["true_peak_dbtp"] < -1.0
