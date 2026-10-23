from types import SimpleNamespace
from io import BytesIO

import numpy as np
import pytest

from clipforge import auto_layout, captions
from clipforge.storage import PipelineError

HEIGHT = 180
WIDTH = 320


def texture(rows, cols, seed=0, amplitude=255):
    rng = np.random.default_rng(seed)
    return rng.integers(0, amplitude, size=(rows, cols), dtype=np.uint8)


def frame_with(regions, count=4, base=128):
    """Flat frames with random texture in the given (row slice, column slice, amplitude) regions."""
    frames = np.full((count, HEIGHT, WIDTH), base, dtype=np.uint8)
    for rows, cols, amplitude in regions:
        shape = (rows.stop - rows.start, cols.stop - cols.start)
        frames[:, rows, cols] = texture(
            *shape, seed=amplitude + rows.start, amplitude=amplitude
        )
    return frames


def test_detail_on_the_right_selects_right_window():
    frames = frame_with([(slice(0, HEIGHT), slice(240, WIDTH), 255)])
    crop = auto_layout.choose_crop(frames)
    assert crop["anchor"] == "right"
    assert crop["x"] == pytest.approx(0.25)
    assert crop["kept"]["right"] > crop["kept"]["center"] > 0


def test_detail_on_the_left_selects_left_window():
    frames = frame_with([(slice(0, HEIGHT), slice(0, 80), 255)])
    crop = auto_layout.choose_crop(frames)
    assert crop["anchor"] == "left"
    assert crop["x"] == 0.0
    assert crop["kept"]["left"] > crop["kept"]["right"]


def test_uniform_detail_selects_center_window():
    frames = frame_with([(slice(0, HEIGHT), slice(0, WIDTH), 255)])
    crop = auto_layout.choose_crop(frames)
    assert crop["anchor"] == "center"
    assert crop["x"] == pytest.approx(0.125)
    assert crop["kept"]["center"] == pytest.approx(0.75, abs=0.02)


def test_flat_frames_select_center_without_dividing_by_zero():
    frames = np.full((3, HEIGHT, WIDTH), 90, dtype=np.uint8)
    crop = auto_layout.choose_crop(frames)
    assert crop["anchor"] == "center"
    assert crop["kept"] == {"left": 0.75, "center": 0.75, "right": 0.75}


def test_keep_one_leaves_no_choice():
    frames = frame_with([(slice(0, HEIGHT), slice(240, WIDTH), 255)])
    crop = auto_layout.choose_crop(frames, keep=1.0)
    assert crop["x"] == 0.0
    assert crop["keep"] == 1.0


def test_dark_column_with_sparse_text_is_not_detail():
    # Left: dense but low-contrast texture. Right: a black panel with sparse bright text.
    rng = np.random.default_rng(1)
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[:, :, 0:80] = rng.integers(100, 150, size=(4, HEIGHT, 80), dtype=np.uint8)
    panel = np.zeros((4, HEIGHT, 80), dtype=np.uint8)
    panel[rng.random((4, HEIGHT, 80)) < 0.08] = 255
    frames[:, :, 240:WIDTH] = panel
    crop = auto_layout.choose_crop(frames)
    # Raw energy prefers the text panel; the dark-column correction does not.
    assert max(crop["raw_kept"], key=crop["raw_kept"].get) == "right"
    assert crop["anchor"] == "left"
    assert crop["kept"]["right"] < 0.1


def test_dark_run_away_from_the_edge_is_not_an_overlay():
    rng = np.random.default_rng(3)
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[:, :, 0:80] = rng.integers(100, 150, size=(4, HEIGHT, 80), dtype=np.uint8)
    frames[:, :, 120:160] = 0
    crop = auto_layout.choose_crop(frames)
    assert crop["overlay"]["side"] is None
    assert crop["kept"] == crop["raw_kept"]


def test_dark_run_wider_than_the_limit_is_not_an_overlay():
    # A dark run covering 140 of 320 columns (over 40%) reads as a dark scene, not an overlay.
    rng = np.random.default_rng(4)
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[:, :, 0:100] = rng.integers(100, 150, size=(4, HEIGHT, 100), dtype=np.uint8)
    frames[:, :, 180:WIDTH] = 0
    crop = auto_layout.choose_crop(frames)
    assert crop["overlay"]["side"] is None
    assert crop["kept"] == crop["raw_kept"]


def test_window_moves_clear_of_an_edge_overlay():
    # Detail spans columns 40..200 and a black panel fills columns 240..320. Every window
    # containing the detail ties, so the center is chosen; its right edge (280) reaches the
    # panel, so the window moves to end at the panel's edge (x = 0).
    rng = np.random.default_rng(5)
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[:, :, 40:200] = rng.integers(0, 256, size=(4, HEIGHT, 160), dtype=np.uint8)
    frames[:, :, 240:WIDTH] = 0
    crop = auto_layout.choose_crop(frames)
    assert crop["overlay"] == {"side": "right", "start": 0.75, "end": 1.0}
    assert crop["x"] == 0.0
    assert crop["anchor"] == "left"


def test_window_stays_when_the_overlay_cannot_be_cleared():
    # A 30% panel at the right edge starts at column 224: a 240-column window cannot end
    # before it, so the window is not moved.
    rng = np.random.default_rng(6)
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[:, :, 40:200] = rng.integers(0, 256, size=(4, HEIGHT, 160), dtype=np.uint8)
    frames[:, :, 224:WIDTH] = 0
    crop = auto_layout.choose_crop(frames)
    assert crop["overlay"]["side"] == "right"
    assert crop["x"] == pytest.approx(0.125)


def test_detail_between_the_edges_gives_an_offset_window():
    rng = np.random.default_rng(2)
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[:, :, 20:60] = rng.integers(0, 256, size=(4, HEIGHT, 40), dtype=np.uint8)
    crop = auto_layout.choose_crop(frames)
    assert crop["anchor"] == "offset"
    assert 0 < crop["x"] < 1 - crop["keep"]
    # Among equally good offsets, the one nearest the center is taken.
    assert crop["x"] == pytest.approx(10 / WIDTH)


CROP = {"anchor": "center", "x": 0.125, "keep": 0.75, "kept": {}}
TIMES = [0.0, 0.5, 1.0, 1.5]
BASE = captions.FILL_BOTTOM_SHARE


def test_busy_band_at_the_bottom_lifts_caption_just_above_it():
    frames = frame_with([(slice(140, HEIGHT), slice(0, WIDTH), 255)])
    lifts = auto_layout.caption_lifts(TIMES, frames, [(0.0, 0.5)], CROP)
    # The band's top row is 139 or 140 (the gradient reaches one row up); the caption clears
    # the band by LIFT_PAD.
    assert lifts[0] == pytest.approx((HEIGHT - 140) / HEIGHT + 0.015, abs=0.01)


def test_quiet_frames_keep_the_base_margin():
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 1.0)], CROP) == [BASE]


def test_band_below_the_base_keeps_the_base_margin():
    frames = frame_with([(slice(170, HEIGHT), slice(0, WIDTH), 255)])
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 0.5)], CROP) == [BASE]


def test_band_outside_the_search_range_is_ignored():
    frames = frame_with([(slice(0, 100), slice(0, WIDTH), 255)])
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 1.0)], CROP) == [BASE]


def test_lift_is_capped_at_max_lift():
    frames = frame_with([(slice(100, HEIGHT), slice(0, WIDTH), 255)])
    # The band reaches above the 40 percent search range, so the lift is the cap.
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 0.5)], CROP) == [0.32]
    assert auto_layout.caption_lifts(
        TIMES, frames, [(0.0, 0.5)], CROP, max_lift=0.3
    ) == [0.3]


def test_uniform_busy_rows_are_not_a_band():
    # Every row is equally busy, so none is busier than the window's median row.
    frames = frame_with([(slice(0, HEIGHT), slice(240, WIDTH), 255)])
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 1.0)], CROP) == [BASE]


def test_detail_in_one_half_of_the_window_lifts_the_caption():
    background = texture(HEIGHT, WIDTH, seed=9, amplitude=40)
    frames = np.repeat(background[None], 4, axis=0)
    # A moderate band in the right half only: the whole window's row density stays under the
    # busy level, but the right half's crosses it (a wipe in the corner works this way).
    frames[:, 140:, 200:280] = texture(HEIGHT - 140, 80, seed=4, amplitude=80)
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 0.5)], CROP)[0] > BASE


def test_chat_overlay_columns_do_not_count_as_busy():
    background = texture(HEIGHT, WIDTH, seed=9, amplitude=40)
    frames = np.repeat(background[None], 4, axis=0)
    # Detail only under the overlay, the right 40 columns of the window.
    frames[:, 140:, 240:280] = texture(HEIGHT - 140, 40, seed=10)
    overlay = {"side": "right", "start": 0.75, "end": 1.0}
    with_overlay = {**CROP, "overlay": overlay}
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 0.5)], CROP)[0] > BASE
    assert auto_layout.caption_lifts(TIMES, frames, [(0.0, 0.5)], with_overlay) == [
        BASE
    ]


def test_captions_close_in_sequence_share_the_largest_lift():
    # Detail in rows 140 and below only in the first 5 seconds (frames 0 to 9 of 40).
    frames = np.full((40, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[0:10, 140:, :] = texture(HEIGHT - 140, WIDTH, seed=5)
    times = [0.5 * index for index in range(40)]
    # The first caption sees the detail in most of its frames; the second, 1.5 s later, does not.
    events = [(3.0, 3.5), (5.0, 5.5), (15.0, 15.5)]
    lifts = auto_layout.caption_lifts(times, frames, events, CROP)
    assert lifts[0] > BASE and lifts[1] == lifts[0] and lifts[2] == BASE


def test_detail_in_few_frames_of_a_caption_is_not_persistent():
    frames = np.full((10, HEIGHT, WIDTH), 128, dtype=np.uint8)
    # Detail in only 3 of the 8 frames near the caption: the median gradient is zero there.
    frames[0:3, 140:, :] = texture(HEIGHT - 140, WIDTH, seed=6)
    times = [0.5 * index for index in range(10)]
    assert auto_layout.caption_lifts(times, frames, [(0.0, 0.5)], CROP) == [BASE]


def test_caption_outside_the_frames_uses_the_nearest_frames():
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[:, 137:, :] = texture(HEIGHT - 137, WIDTH, seed=3)
    lifts = auto_layout.caption_lifts(
        TIMES, frames, [(100.0, 101.0), (-9.0, -8.0)], CROP
    )
    assert lifts[0] > BASE and lifts[1] > BASE


def test_lifts_need_one_frame_set_per_sample_and_a_crop_window():
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    with pytest.raises(ValueError, match="same length"):
        auto_layout.caption_lifts(TIMES[:3], frames, [(0.0, 1.0)], CROP)
    with pytest.raises(PipelineError, match="no frames"):
        auto_layout.caption_lifts([], np.zeros((0, HEIGHT, WIDTH)), [], CROP)


def test_band_top_stops_at_a_quiet_stretch_longer_than_the_tolerance():
    busy = np.zeros(20, dtype=bool)
    busy[15:20] = True
    busy[10:12] = True  # three quiet rows (12 to 14) between the two bands
    assert auto_layout._band_top(busy, 1.0, 3) == 10
    assert auto_layout._band_top(busy, 1.0, 2) == 15


def test_band_top_searches_only_the_bottom_share():
    busy = np.zeros(20, dtype=bool)
    busy[2:9] = True
    assert auto_layout._band_top(busy, 0.5, 3) is None
    busy[10:20] = True
    assert auto_layout._band_top(busy, 0.5, 3) == 10


def test_runs_of_captions_take_the_largest_value():
    events = [(0.0, 1.0), (1.5, 2.5), (4.5, 5.5)]
    assert auto_layout._shared_max(events, [0.2, 0.12, 0.12]) == [0.2, 0.2, 0.12]


def fake_decoder(monkeypatch, data, code=0):
    seen = {}

    def popen(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(
            stdout=BytesIO(data),
            wait=lambda: code,
            poll=lambda: code,
            kill=lambda: None,
        )

    monkeypatch.setattr(auto_layout.subprocess, "Popen", popen)
    return seen


def test_sample_frames_reads_rawvideo_with_source_times(monkeypatch):
    data = np.arange(5 * 3 * 4, dtype=np.uint8).reshape(5, 3, 4)
    seen = fake_decoder(monkeypatch, data.tobytes() + b"\x01\x02")
    blocks = list(auto_layout.sample_frames("source.mp4", 10, 12.5, width=4, height=3))
    assert len(blocks) == 1
    times, frames = blocks[0]
    assert times == [10.0, 10.5, 11.0, 11.5, 12.0]
    assert frames.dtype == np.uint8
    assert np.array_equal(frames, data)
    command = seen["command"]
    assert command[command.index("-ss") + 1] == "10.000"
    assert command[command.index("-vf") + 1] == "fps=2,scale=4:3"
    assert command[-1] == "pipe:1"


def test_sample_frames_without_frames_is_an_error(monkeypatch):
    fake_decoder(monkeypatch, b"")
    with pytest.raises(PipelineError):
        list(auto_layout.sample_frames("source.mp4", 0, 5, width=4, height=3))


def test_sample_frames_stays_bounded_and_checks_decoder_failure(monkeypatch):
    data = np.arange(5 * 12, dtype=np.uint8).reshape(5, 3, 4)
    monkeypatch.setattr(auto_layout, "BLOCK", 2)
    fake_decoder(monkeypatch, data.tobytes())
    blocks = list(auto_layout.sample_frames("source.mp4", 0, 3, width=4, height=3))
    assert [len(frames) for _, frames in blocks] == [2, 2, 1]
    assert np.array_equal(np.concatenate([frames for _, frames in blocks]), data)
    fake_decoder(monkeypatch, data.tobytes(), code=1)
    with pytest.raises(PipelineError, match="failed"):
        list(auto_layout.sample_frames("source.mp4", 0, 3, width=4, height=3))


def test_sample_window_must_be_forward():
    with pytest.raises(ValueError):
        auto_layout.sample_frames("source.mp4", 5, 5)


def test_plan_combines_the_steps(monkeypatch):
    frames = frame_with([(slice(0, HEIGHT), slice(240, WIDTH), 255)], count=4)
    times = [0.0, 0.5, 1.0, 1.5]
    monkeypatch.setattr(
        auto_layout, "sample_frames", lambda *args, **kwargs: (times, frames)
    )
    result = auto_layout.plan("source.mp4", 0, 2, [(0.0, 1.0)])
    assert result["crop"]["anchor"] == "right"
    # Every row is equally busy in the window, so no band stands out.
    assert result["lifts"] == [pytest.approx(captions.FILL_BOTTOM_SHARE)]
    assert result["frames"] == 4
    assert result["seconds"] >= 0


@pytest.mark.parametrize("side", ["left", "right"])
def test_persistent_corner_camera_is_kept_even_when_center_is_busier(side):
    rng = np.random.default_rng(81)
    frames = rng.integers(80, 150, size=(8, HEIGHT, WIDTH), dtype=np.uint8)
    left, right = (3, 70) if side == "left" else (250, 317)
    top, bottom = 110, 175
    frames[:, top : bottom + 1, left : left + 2] = 255
    frames[:, top : bottom + 1, right : right + 2] = 255
    frames[:, top : top + 2, left : right + 1] = 255
    frames[:, bottom : bottom + 2, left : right + 1] = 255
    crop = auto_layout.choose_crop(frames)
    assert crop["wipe"] is not None
    assert crop["wipe"]["side"] == side
    assert crop["x"] <= crop["wipe"]["left"]
    assert crop["x"] + crop["keep"] >= crop["wipe"]["right"]


def test_transient_rectangle_is_not_a_camera_window():
    rng = np.random.default_rng(82)
    frames = rng.integers(80, 150, size=(8, HEIGHT, WIDTH), dtype=np.uint8)
    frames[0, 110:177, 250:320] = 255
    assert auto_layout.choose_crop(frames)["wipe"] is None


def test_caption_density_excludes_source_chat_columns():
    frames = np.full((4, HEIGHT, WIDTH), 100, dtype=np.uint8)
    frames[:, :, 240:] = 0
    frames[:, 135:, 240:] = texture(45, 80, seed=9)
    crop = {
        "x": 0.0,
        "keep": 1.0,
        "overlay": {"side": "right", "start": 0.75, "end": 1.0},
    }
    assert auto_layout.caption_lifts([0, 0.5, 1, 1.5], frames, [(0, 1)], crop) == [
        captions.FILL_BOTTOM_SHARE
    ]


def test_streamed_statistics_match_single_batch():
    frames = frame_with([(slice(100, 180), slice(10, 200), 255)], count=10)
    times = [i / 2 for i in range(10)]
    streamed = auto_layout.frame_statistics(
        ((times[i : i + 3], frames[i : i + 3]) for i in range(0, 10, 3))
    )
    assert streamed.count == 10
    assert auto_layout.choose_crop(streamed) == auto_layout.choose_crop(frames)
    crop = auto_layout.choose_crop(streamed)
    events = [(0, 1), (3, 4)]
    blocks = ((times[i : i + 3], frames[i : i + 3]) for i in range(0, 10, 3))
    assert auto_layout._stream_lifts(
        streamed, blocks, events, crop
    ) == auto_layout.caption_lifts(times, frames, events, crop)


def test_streamed_persistent_lifts_match_moving_scene_and_reject_changed_decode():
    rng = np.random.default_rng(101)
    frames = rng.integers(80, 150, size=(80, HEIGHT, WIDTH), dtype=np.uint8)
    frames[:, 145:, :160] = texture(35, 160, seed=4)
    times = [i / 2 for i in range(len(frames))]
    stats = auto_layout.frame_statistics((times, frames))
    crop = {"x": 0.0, "keep": 1.0}
    events = [(0, 1), (10, 12), (35, 38), (50, 51)]
    blocks = ((times[i : i + 7], frames[i : i + 7]) for i in range(0, len(frames), 7))
    assert auto_layout._stream_lifts(
        stats, blocks, events, crop
    ) == auto_layout.caption_lifts(times, frames, events, crop)
    with pytest.raises(PipelineError, match="changed"):
        auto_layout._stream_lifts(stats, [(times[:-1], frames[:-1])], events, crop)
