"""Reducing an ROI's pixels to one brightness number.

This is the hot path: it runs once per segment per frame. numpy only, no OpenCV.

The choice of metric matters more than it looks. Under heavy underexposure an
energised LED saturates its own colour channel while the others stay near zero, so a
plain RGB average of a fully lit red segment lands around 85 -- *below* a threshold of
90 that a green segment clears easily. Taking the per-pixel maximum channel instead
makes any saturated hue read near 255, which is what "identify segments by position,
not hue" actually requires in practice.
"""

from __future__ import annotations

import numpy as np

from .config import Roi

#: Rec. 601 luminance weights, for the `luma` metric.
_LUMA_WEIGHTS = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def measure(data: np.ndarray, roi: Roi, metric: str = "value") -> float:
    """Mean brightness inside ``roi``, in the range 0-255.

    ``data`` is an ``(H, W, 3)`` uint8 RGB frame.
    """
    rows, cols = roi.slices
    patch = data[rows, cols]
    if patch.size == 0:
        raise ValueError(f"roi {roi.as_list()} selects no pixels from a {data.shape} frame")

    if metric == "value":
        # Per-pixel max channel: hue-independent, so one threshold serves every segment.
        return float(patch.max(axis=2).mean())
    if metric == "luma":
        return float((patch.reshape(-1, 3) @ _LUMA_WEIGHTS).mean())
    if metric == "mean_rgb":
        return float(patch.mean())
    raise ValueError(f"unknown brightness metric {metric!r}")


def measure_stats(data: np.ndarray, roi: Roi, metric: str = "value") -> tuple[float, float, float]:
    """``(mean, minimum, maximum)`` brightness inside ``roi``.

    The calibration UI shows all three: a high maximum with a low mean is the signature
    of an ROI that has slipped half off the segment.
    """
    rows, cols = roi.slices
    patch = data[rows, cols]
    if patch.size == 0:
        raise ValueError(f"roi {roi.as_list()} selects no pixels from a {data.shape} frame")

    if metric == "value":
        reduced = patch.max(axis=2).astype(np.float32)
    elif metric == "luma":
        reduced = patch.astype(np.float32) @ _LUMA_WEIGHTS
    elif metric == "mean_rgb":
        reduced = patch.mean(axis=2, dtype=np.float32)
    else:
        raise ValueError(f"unknown brightness metric {metric!r}")
    return float(reduced.mean()), float(reduced.min()), float(reduced.max())
