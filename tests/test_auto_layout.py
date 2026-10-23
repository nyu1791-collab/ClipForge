from types import SimpleNamespace

import numpy as np
import pytest

from clipforge import auto_layout, storage
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


def test_bottom_band_detail_moves_caption_to_top():
    frames = frame_with([(slice(137, HEIGHT), slice(0, WIDTH), 255)])
    # Only the caption's own frames are busy below; the rest of the clip is quiet.
    frames[2:] = 128
    crop = {"anchor": "center", "x": 0.125, "keep": 0.75, "kept": {}}
    positions = auto_layout.caption_positions(
        [0.0, 0.5, 1.0, 1.5], frames, [(0.0, 0.5)], crop
    )
    assert positions == ["top"]


def test_no_bottom_detail_keeps_caption_at_bottom():
    frames = frame_with([(slice(0, 100), slice(0, WIDTH), 255)])
    crop = {"anchor": "center", "x": 0.125, "keep": 0.75, "kept": {}}
    positions = auto_layout.caption_positions(
        [0.0, 0.5, 1.0, 1.5], frames, [(0.0, 1.0)], crop
    )
    assert positions == ["bottom"]


def test_equally_busy_bands_keep_caption_at_bottom():
    frames = frame_with(
        [
            (slice(0, 40), slice(0, WIDTH), 255),
            (slice(140, HEIGHT), slice(0, WIDTH), 255),
        ]
    )
    crop = {"anchor": "center", "x": 0.125, "keep": 0.75, "kept": {}}
    positions = auto_layout.caption_positions(
        [0.0, 0.5, 1.0, 1.5], frames, [(0.0, 0.5)], crop
    )
    assert positions == ["bottom"]


def test_quiet_bottom_below_floor_keeps_caption_at_bottom():
    frames = np.full((10, HEIGHT, WIDTH), 128, dtype=np.uint8)
    # A strong bottom band in most of the clip makes the average floor high.
    frames[5:] = frame_with([(slice(137, HEIGHT), slice(0, WIDTH), 255)], count=5)
    # The caption's own frames have only a faint bottom texture: busier than the top, but quiet.
    frames[0:2, 137:, :] = 128 + texture(HEIGHT - 137, WIDTH, seed=7, amplitude=3)
    frames[0:2, :40, :] = 128
    crop = {"anchor": "center", "x": 0.125, "keep": 0.75, "kept": {}}
    times = [0.5 * index for index in range(10)]
    assert auto_layout.caption_positions(times, frames, [(0.0, 0.5)], crop) == [
        "bottom"
    ]


def test_caption_outside_the_frames_uses_the_nearest_frame():
    frames = np.full((4, HEIGHT, WIDTH), 128, dtype=np.uint8)
    frames[3, 137:, :] = texture(HEIGHT - 137, WIDTH, seed=3)
    crop = {"anchor": "center", "x": 0.125, "keep": 0.75, "kept": {}}
    times = [0.0, 0.5, 1.0, 1.5]
    positions = auto_layout.caption_positions(
        times, frames, [(100.0, 101.0), (-9.0, -8.0)], crop
    )
    assert positions == ["top", "bottom"]
    frames[0, 137:, :] = texture(HEIGHT - 137, WIDTH, seed=4)
    frames[1:, 137:, :] = 128
    positions = auto_layout.caption_positions(times, frames, [(-9.0, -8.0)], crop)
    assert positions == ["top"]


def test_short_runs_of_captions_share_the_majority_band():
    events = [(0.0, 1.0), (1.5, 2.5), (3.0, 4.0)]
    raw = ["top", "bottom", "top"]
    assert auto_layout._steady(events, raw) == ["top", "top", "top"]


def test_tied_run_keeps_the_default_band():
    assert auto_layout._steady([(0.0, 1.0), (1.5, 2.5)], ["top", "bottom"]) == [
        "bottom",
        "bottom",
    ]


def test_separated_captions_keep_their_own_band():
    events = [(0.0, 1.0), (4.0, 5.0)]
    assert auto_layout._steady(events, ["top", "bottom"]) == ["top", "bottom"]


def test_sample_frames_reads_rawvideo_with_source_times(monkeypatch):
    data = np.arange(5 * 3 * 4, dtype=np.uint8).reshape(5, 3, 4)
    seen = {}

    def fake_process(command, timeout=180, stage=None, cwd=None):
        seen.update(command=command, timeout=timeout, stage=stage)
        # A trailing partial frame is dropped.
        return SimpleNamespace(stdout=data.tobytes() + b"\x01\x02")

    monkeypatch.setattr(storage, "process", fake_process)
    times, frames = auto_layout.sample_frames("source.mp4", 10, 12.5, width=4, height=3)

    assert times == [10.0, 10.5, 11.0, 11.5, 12.0]
    assert frames.shape == (5, 3, 4)
    assert frames.dtype == np.uint8
    assert np.array_equal(frames, data)
    assert seen["stage"] == "layout-frames"
    command = seen["command"]
    assert command[command.index("-ss") + 1] == "10.000"
    assert command[command.index("-t") + 1] == "2.500"
    assert command[command.index("-vf") + 1] == "fps=2,scale=4:3"
    assert command[command.index("-pix_fmt") + 1] == "gray"
    assert command[-1] == "pipe:1"


def test_sample_frames_without_frames_is_an_error(monkeypatch):
    monkeypatch.setattr(
        storage, "process", lambda command, **kwargs: SimpleNamespace(stdout=b"")
    )
    with pytest.raises(PipelineError):
        auto_layout.sample_frames("source.mp4", 0, 5, width=4, height=3)


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
    assert result["positions"] == ["bottom"]
    assert result["frames"] == 4
    assert result["seconds"] >= 0
