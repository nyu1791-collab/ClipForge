import shutil
import subprocess
from types import SimpleNamespace

import pytest

from clipforge import hardware, media
from clipforge.models import SelectedClip, Settings, SoundEffect, Zoom
from clipforge.storage import PipelineError

CLIP = SimpleNamespace(start=10.0, end=20.0)
KEEP = [(10.0, 12.0), (13.0, 20.0)]


def make(**overrides):
    return Settings(
        **{
            "gemini_model": "manual-no-api",
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


def test_crop_expression_uses_normalized_box():
    assert media._box((0.1, 0.2, 0.5, 0.25)) == (
        "crop=iw*0.500000:ih*0.250000:iw*0.100000:ih*0.200000"
    )


def test_timelines_join_kept_regions_only_when_cut():
    assert media._video_timeline(CLIP, None) == "[0:V:0]setpts=PTS-STARTPTS[vin]"
    assert media._audio_timeline(CLIP, None) == "[0:a:0]asetpts=PTS-STARTPTS[ain]"
    video = media._video_timeline(CLIP, KEEP)
    assert "trim=start=0.000000:end=2.000000" in video
    assert "trim=start=3.000000:end=10.000000" in video
    assert video.endswith("concat=n=2:v=1:a=0[vin]")
    assert media._audio_timeline(CLIP, KEEP).endswith("concat=n=2:v=0:a=1[ain]")


def test_each_kept_audio_piece_fades_at_both_ends_to_hide_splices():
    audio = media._audio_timeline(CLIP, KEEP)
    # First piece is 2 s long, second is 7 s; both use the full splice fade.
    assert "atrim=start=0.000000:end=2.000000,asetpts=PTS-STARTPTS," in audio
    assert "afade=t=in:st=0:d=0.015,afade=t=out:st=1.985:d=0.015[ak0]" in audio
    assert "afade=t=in:st=0:d=0.015,afade=t=out:st=6.985:d=0.015[ak1]" in audio


def test_a_very_short_kept_piece_fades_by_at_most_half_its_length():
    audio = media._audio_timeline(CLIP, [(10.0, 10.02)])
    assert "afade=t=in:st=0:d=0.010,afade=t=out:st=0.010:d=0.010[ak0]" in audio


def test_layouts_end_with_the_requested_label():
    box = (0.0, 0.0, 1.0, 1.0)
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
    panels = media._layout(
        make(layout="panels", face_box=(0.3, 0.1, 0.4, 0.4), game_box=box),
        404,
        720,
        "vin",
        "out",
    )
    assert "vstack=inputs=2" in panels and panels.endswith("[out]")


def test_overlay_positions_cover_each_named_place():
    assert (
        media._position("right", 10, 100) == "main_w-overlay_w-10:(main_h-overlay_h)/2"
    )
    assert media._position("top", 10, 100) == "(main_w-overlay_w)/2:100"
    assert (
        media._position("center", 10, 100)
        == "(main_w-overlay_w)/2:(main_h-overlay_h)/2"
    )
    assert (
        media._position("bottom", 10, 100) == "(main_w-overlay_w)/2:main_h-overlay_h-10"
    )


def test_video_graph_chains_zoom_visuals_fade_progress_and_captions():
    settings = make(
        face_box=(0.1, 0.1, 0.5, 0.5),
        zoom=[Zoom(start=1, end=3, scale=1.5)],
        heading=True,
        progress_bar=True,
        fade_seconds=0.5,
    )
    visuals = [
        {"file": "v.png", "start": 1, "end": 4, "position": "top", "max_fraction": 0.5}
    ]
    graph = media._video_graph(
        CLIP, settings, 404, 720, None, 10.0, visuals, "captions.ass"
    )
    top = round(720 * 0.14)  # below the heading when one is shown
    assert "split=2[b0][b1]" in graph
    assert "enable='between(t,1.000,3.000)'" in graph
    assert "[1:v]format=rgba" in graph
    assert f"overlay=(main_w-overlay_w)/2:{top}" in graph
    assert "fade=t=in:st=0:d=0.500" in graph
    # The overlay itself fades in at its start and out before its end (alpha only).
    assert "fade=t=in:st=1.000:d=0.500:alpha=1" in graph
    assert "fade=t=out:st=3.500:d=0.500:alpha=1" in graph
    assert "color=c=black@0:s=404x8:r=30:d=10.000000" in graph
    assert "overlay=0:712:shortest=1[pb]" in graph
    assert graph.endswith("ass=filename=captions.ass[vout]")


def test_short_overlays_fade_by_at_most_half_their_length():
    visuals = [
        {
            "file": "v.png",
            "start": 2,
            "end": 2.4,
            "position": "center",
            "max_fraction": 0.5,
        }
    ]
    graph = media._video_graph(
        CLIP,
        make(fade_seconds=0.5, progress_bar=False),
        404,
        720,
        None,
        10.0,
        visuals,
        None,
    )
    assert "fade=t=in:st=2.000:d=0.200:alpha=1" in graph
    assert "fade=t=out:st=2.200:d=0.200:alpha=1" in graph
    still = media._video_graph(
        CLIP,
        make(fade_seconds=0, progress_bar=False),
        404,
        720,
        None,
        10.0,
        visuals,
        None,
    )
    assert "alpha=1" not in still


def test_plain_video_graph_has_no_optional_stages():
    settings = make(progress_bar=False, fade_seconds=0, layout="fit")
    graph = media._video_graph(CLIP, settings, 404, 720, None, 10.0, [], None)
    assert "split=" not in graph and "fade=" not in graph and "geq=" not in graph
    assert graph.endswith("null[vout]")


def test_extra_inputs_are_ordered_visuals_then_music_then_effects():
    settings = make(
        bgm="/local/bg.mp3",
        sound_effects=[SoundEffect(file="/local/fx.wav", time=1.0)],
    )
    visuals = [{"file": "/local/v.png"}]
    args = media._extra_inputs(settings, 10.0, visuals)
    inputs = [args[i + 1] for i, value in enumerate(args) if value == "-i"]
    assert inputs == ["/local/v.png", "/local/bg.mp3", "/local/fx.wav"]
    assert args[args.index("-stream_loop") + 1] == "-1"
    # Every extra input gets the same local-only protocol whitelist as the source.
    assert args.count("-protocol_whitelist") == len(inputs)


def test_audio_graph_ducks_music_only_when_narration_exists():
    settings = make(bgm="/local/bg.mp3", ducking=True)
    ducked = media._audio_graph(CLIP, settings, None, 10.0, True, first_extra=1)
    assert "sidechaincompress=threshold=0.03:ratio=6:attack=20:release=300" in ducked
    assert "asplit=2[nk][nm]" in ducked
    silent = media._audio_graph(CLIP, settings, None, 10.0, False, first_extra=1)
    assert "anullsrc" in silent and "sidechaincompress" not in silent
    plain = media._audio_graph(
        CLIP, make(bgm="/b.mp3", ducking=False), None, 10.0, True, 1
    )
    assert "sidechaincompress" not in plain and "[bg0]anull[bgd]" in plain


def test_sound_effects_are_delayed_to_their_time_and_mixed():
    settings = make(sound_effects=[SoundEffect(file="/fx.wav", time=2.5, gain_db=-9)])
    graph = media._audio_graph(CLIP, settings, KEEP, 10.0, True, first_extra=1)
    assert "adelay=2500:all=1" in graph
    assert "volume=-9.00dB" in graph
    assert "amix=inputs=2:duration=first:normalize=0[mix]" in graph
    assert "atrim=start=0.000000:end=2.000000" in graph  # narration follows the cut


def test_narration_only_mix_passes_through():
    graph = media._audio_graph(CLIP, make(), None, 10.0, True, first_extra=1)
    assert graph.endswith("[nm]anull[mix]")


def test_loudness_arguments_use_the_first_pass_measurement():
    assert media._loudness_args(None) == "loudnorm=I=-16:TP=-1.5:LRA=11"
    measured = {
        "input_i": -20.1,
        "input_tp": -3.2,
        "input_lra": 5.0,
        "input_thresh": -30.0,
        "target_offset": 0.4,
    }
    assert media._loudness_args(measured) == (
        "loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=-20.10:measured_TP=-3.20:"
        "measured_LRA=5.00:measured_thresh=-30.00:offset=0.40:linear=true"
    )


def test_audio_tail_fades_only_when_the_clip_is_long_enough():
    tail = media._audio_tail(make(fade_seconds=0.3), 10.0, None)
    assert "afade=t=in:st=0:d=0.300" in tail and "afade=t=out:st=9.700:d=0.300" in tail
    assert tail.endswith("[aout]")
    assert "afade" not in media._audio_tail(make(fade_seconds=0), 10.0, None)
    assert "afade" not in media._audio_tail(make(fade_seconds=0.3), 0.5, None)


def test_measure_loudness_parses_the_first_pass_json(monkeypatch):
    seen = {}
    stderr = (
        'noise {"input_i": "-20.5", "input_tp": "-2.1", "input_lra": "6.2", '
        '"input_thresh": "-31.0", "target_offset": "0.25"}'
    )

    def fake(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "", stderr)

    monkeypatch.setattr(media.subprocess, "run", fake)
    values = media.measure_loudness(
        "/in.mp4", CLIP, make(threads=2, encode_threads=6), KEEP, 8.0
    )
    assert values["input_i"] == -20.5 and values["target_offset"] == 0.25
    assert "-n" in seen["command"]
    assert seen["command"][-3:] == ["-f", "null", "-"]
    assert seen["command"][seen["command"].index("-filter_complex_threads") + 1] == "6"


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "", "{}"),
        subprocess.CompletedProcess([], 0, "", "no json here"),
    ],
)
def test_measure_loudness_rejects_failed_or_unusable_output(monkeypatch, result):
    monkeypatch.setattr(media.subprocess, "run", lambda command, **kwargs: result)
    with pytest.raises(PipelineError, match="loudness measurement"):
        media.measure_loudness("/in.mp4", CLIP, make(), None, 10.0)


def test_silent_audio_falls_back_to_a_single_loudness_pass(monkeypatch):
    silent = (
        '{"input_i": "-inf", "input_tp": "-56.30", "input_lra": "0.00", '
        '"input_thresh": "-70.00", "target_offset": "1.85"}'
    )
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", silent),
    )
    assert media.measure_loudness("/in.mp4", CLIP, make(), None, 10.0) is None


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


def test_required_tools_include_fontconfig_and_libass(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(PipelineError, match="ffmpeg is not installed"):
        media.require_tools()
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        media,
        "process",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, b" T.. ass  ass\n", b""
        ),
    )
    media.require_tools()
    monkeypatch.setattr(
        media,
        "process",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, b" T.. subtitles\n", b""
        ),
    )
    with pytest.raises(PipelineError, match="libass"):
        media.require_tools()
    media.require_tools(captions=False)


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
    monkeypatch.setattr(media.subprocess, "run", fake)
    media.render(
        "/in.mp4",
        tmp_path / "out.mp4",
        clip_for_render(),
        make(
            height=720,
            threads=2,
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
        make(threads=2, encode_threads=6, video_encoder="libx264"),
    )
    command = seen["command"]
    assert command.count("-threads") == 2
    assert "-filter_threads" in command
    for i, option in enumerate(command):
        if option in {"-threads", "-filter_threads"}:
            assert command[i + 1] == "6"


@pytest.mark.parametrize(
    ("settings_update", "message"),
    [
        (
            {"face_box": (0.1, 0.1, 0.5, 0.5), "zoom": [Zoom(start=1, end=50)]},
            "zoom interval",
        ),
        ({"bgm": "/definitely/missing.mp3"}, "background music"),
        (
            {"sound_effects": [SoundEffect(file="/definitely/missing.wav", time=1)]},
            "sound effect file",
        ),
        ({"sound_effects": [SoundEffect(file=__file__, time=12)]}, "sound effect time"),
    ],
)
def test_render_refuses_inputs_outside_the_clip_or_missing(
    tmp_path, probed, settings_update, message
):
    settings = make(**settings_update)
    with pytest.raises(PipelineError, match=message):
        media.render("/in.mp4", tmp_path / "out.mp4", clip_for_render(), settings)


def test_render_refuses_visuals_outside_the_clip_or_missing(tmp_path, probed):
    late = [
        {
            "file": __file__,
            "start": 1,
            "end": 50,
            "position": "right",
            "max_fraction": 0.4,
        }
    ]
    with pytest.raises(PipelineError, match="visual interval"):
        media.render(
            "/in.mp4", tmp_path / "out.mp4", clip_for_render(), make(), visuals=late
        )
    absent = [
        {
            "file": "/nope.png",
            "start": 0,
            "end": 1,
            "position": "right",
            "max_fraction": 0.4,
        }
    ]
    with pytest.raises(PipelineError, match="visual file is missing"):
        media.render(
            "/in.mp4", tmp_path / "out.mp4", clip_for_render(), make(), visuals=absent
        )


def test_render_refuses_silence_cuts_with_too_many_regions(tmp_path, probed):
    keep = [(10 + i * 0.15, 10 + i * 0.15 + 0.1) for i in range(61)]
    with pytest.raises(PipelineError, match="too many regions"):
        media.render(
            "/in.mp4", tmp_path / "out.mp4", clip_for_render(), make(), keep=keep
        )


CHAT_PANEL = {
    "chat_panel": True,
    "aspect": "16:9",
    "height": 1080,
    "chat_panel_width": 480,
}


def test_video_graph_without_the_chat_panel_is_unchanged():
    settings = make(progress_bar=False, fade_seconds=0, layout="fit")
    assert media._video_graph(
        CLIP, settings, 404, 720, None, 10.0, [], "captions.ass"
    ) == (
        "[0:V:0]setpts=PTS-STARTPTS[vin];"
        "[vin]scale=404:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=404:720:(ow-iw)/2:(oh-ih)/2,setsar=1[lay0];"
        "[lay0]ass=filename=captions.ass[vout]"
    )


def test_chat_panel_crops_the_video_left_of_the_column_and_pads_the_right():
    settings = make(**CHAT_PANEL, progress_bar=False, fade_seconds=0)
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, None, 10.0, [], "captions.ass", "chat.ass"
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
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, None, 10.0, [], None, "chat.ass"
    )
    assert (
        "[vin]scale=1440:1080:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,pad=1440:1080:(ow-iw)/2:(oh-ih)/2:color=0x111114,setsar=1[lay0]"
    ) in graph
    assert graph.endswith(
        "[lay0]pad=1920:1080:0:0:color=0x111114,"
        "drawbox=x=1440:y=0:w=2:h=1080:color=0x2a2a30:t=fill,"
        "ass=filename=chat.ass[vout]"
    )


def test_chat_panel_overlays_and_progress_use_the_video_area():
    settings = make(
        **CHAT_PANEL,
        face_box=(0.1, 0.1, 0.5, 0.5),
        zoom=[Zoom(start=1, end=3, scale=1.5)],
        fade_seconds=0,
    )
    visuals = [
        {
            "file": "v.png",
            "start": 1,
            "end": 4,
            "position": "right",
            "max_fraction": 0.5,
        }
    ]
    graph = media._video_graph(
        CLIP, settings, 1920, 1080, None, 10.0, visuals, "captions.ass", "chat.ass"
    )
    # Zoomed face and the visual are sized to the 1440 px area; the visual sits at its right.
    assert "scale=w='min(1440,trunc(iw*486/ih/2)*2)':h=486" in graph
    assert "scale=w='min(iw,720)':h=-2" in graph
    assert "overlay=main_w-overlay_w-43:(main_h-overlay_h)/2" in graph
    assert "color=c=black@0:s=1440x8:r=30:d=10.000000" in graph
    assert r"lte(X\,1440*T/10.000000)" in graph
    assert "overlay=0:1072:shortest=1[pb]" in graph


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


@pytest.mark.parametrize("fit", ["crop", "letterbox"])
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
