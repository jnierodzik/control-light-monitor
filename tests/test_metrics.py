from __future__ import annotations

import numpy as np
import pytest

from stacklight.config import Roi
from stacklight.metrics import measure, measure_stats

ROI = Roi(2, 2, 4, 4)


def frame_with(color, size=(10, 10)) -> np.ndarray:
    data = np.zeros((size[0], size[1], 3), dtype=np.uint8)
    rows, cols = ROI.slices
    data[rows, cols] = color
    return data


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        # This table is the argument for the default metric. A fully lit red LED under
        # a plain RGB mean reads 85 -- below a threshold of 90 -- while the same
        # segment in green would read identically low. Max-channel reads 255 for both.
        ("value", 255.0),
        ("luma", 76.2),
        ("mean_rgb", 85.0),
    ],
)
def test_metrics_disagree_sharply_on_a_saturated_red_led(metric, expected):
    assert measure(frame_with((255, 0, 0)), ROI, metric) == pytest.approx(expected, abs=0.5)


def test_max_channel_is_hue_independent():
    red = measure(frame_with((255, 0, 0)), ROI, "value")
    green = measure(frame_with((0, 255, 0)), ROI, "value")
    blue = measure(frame_with((0, 0, 255)), ROI, "value")
    assert red == green == blue == 255.0


def test_mean_rgb_would_need_different_thresholds_per_colour():
    amber = measure(frame_with((255, 170, 0)), ROI, "mean_rgb")
    red = measure(frame_with((255, 0, 0)), ROI, "mean_rgb")
    assert amber > 90 > red  # one threshold cannot serve both


def test_a_dark_region_reads_near_zero():
    assert measure(np.zeros((10, 10, 3), dtype=np.uint8), ROI, "value") == 0.0


def test_measure_only_looks_inside_the_roi():
    data = np.full((10, 10, 3), 255, dtype=np.uint8)
    rows, cols = ROI.slices
    data[rows, cols] = 0
    assert measure(data, ROI, "value") == 0.0


def test_stats_expose_a_half_aligned_roi():
    """A high maximum with a middling mean is the signature of an ROI that has
    slipped half off its segment -- exactly what the calibration UI must show."""
    data = np.zeros((10, 10, 3), dtype=np.uint8)
    data[2:4, 2:6] = (255, 0, 0)  # top half of the ROI only
    mean, minimum, maximum = measure_stats(data, ROI, "value")
    assert (minimum, maximum) == (0.0, 255.0)
    assert mean == pytest.approx(127.5)


def test_stats_agree_with_measure_on_a_uniform_patch():
    data = frame_with((255, 0, 0))
    mean, minimum, maximum = measure_stats(data, ROI, "value")
    assert mean == minimum == maximum == measure(data, ROI, "value")


def test_unknown_metric_is_rejected():
    with pytest.raises(ValueError, match="unknown brightness metric"):
        measure(frame_with((255, 0, 0)), ROI, "hue")


def test_roi_off_the_frame_is_rejected():
    with pytest.raises(ValueError, match="selects no pixels"):
        measure(np.zeros((4, 4, 3), dtype=np.uint8), Roi(8, 8, 2, 2), "value")
