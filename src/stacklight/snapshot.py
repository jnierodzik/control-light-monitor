"""Encoding a frame as a debug JPEG.

A raw underexposed frame is almost entirely black, which makes it a poor thing to look
at. Two optional treatments make it useful: a brightness gain so the scene around the
LEDs becomes faintly visible, and ROI outlines annotated with the measured value, so a
drifted rectangle is obvious at a glance rather than something to be inferred from
numbers.

Pillow only -- the calibration UI may reach for OpenCV later, but a node publishing
snapshots should not need it.
"""

from __future__ import annotations

import io
import logging

import numpy as np
from PIL import Image, ImageDraw

from .config import NodeConfig, Roi
from .detector import Reading
from .sources.base import Frame

log = logging.getLogger(__name__)

_LIT_OUTLINE = (255, 220, 40)
_UNLIT_OUTLINE = (90, 130, 190)
_DARK_REF_OUTLINE = (200, 80, 200)
_TRIPPED_OUTLINE = (255, 60, 60)


def encode_snapshot(
    frame: Frame,
    config: NodeConfig,
    reading: Reading | None = None,
) -> bytes:
    """Render ``frame`` as a JPEG, scaled and optionally annotated."""
    publish = config.publish
    data = frame.data

    if publish.snapshot_gain != 1.0:
        data = np.clip(data.astype(np.float32) * publish.snapshot_gain, 0, 255).astype(np.uint8)

    image = Image.fromarray(data)
    scale = 1.0
    if image.width > publish.snapshot_max_width:
        scale = publish.snapshot_max_width / image.width
        image = image.resize(
            (publish.snapshot_max_width, max(1, round(image.height * scale))),
            Image.Resampling.BILINEAR,
        )

    if publish.snapshot_annotate:
        _annotate(image, config, reading, scale)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=publish.snapshot_quality, optimize=True)
    return buffer.getvalue()


def _annotate(
    image: Image.Image, config: NodeConfig, reading: Reading | None, scale: float
) -> None:
    draw = ImageDraw.Draw(image)

    for segment in config.segments:
        segment_reading = None if reading is None else reading.segments.get(segment.name)
        lit = segment_reading is not None and segment_reading.lit
        label = segment.name
        if segment_reading is not None:
            label = f"{segment.name} {segment_reading.value:.0f} {segment_reading.mode.value}"
        _box(draw, segment.roi, scale, _LIT_OUTLINE if lit else _UNLIT_OUTLINE, label)

    if config.dark_reference is not None:
        dark = None if reading is None else reading.dark_reference
        tripped = dark is not None and dark.lit
        label = "dark ref" if dark is None else f"dark ref {dark.value:.0f}"
        _box(
            draw,
            config.dark_reference.roi,
            scale,
            _TRIPPED_OUTLINE if tripped else _DARK_REF_OUTLINE,
            label,
        )

    if reading is not None:
        header = f"{config.node_id}  {reading.state or '-'}  {reading.confidence.value}"
        if reading.reasons:
            header += "  " + ",".join(reading.reasons)
        draw.text((4, 4), header, fill=(255, 255, 255))


def _box(
    draw: ImageDraw.ImageDraw,
    roi: Roi,
    scale: float,
    color: tuple[int, int, int],
    label: str,
) -> None:
    x0 = round(roi.x * scale)
    y0 = round(roi.y * scale)
    x1 = round((roi.x + roi.w) * scale)
    y1 = round((roi.y + roi.h) * scale)
    draw.rectangle((x0, y0, x1, y1), outline=color)
    # Labels sit above the box, or below it when the box is against the top edge.
    text_y = y0 - 11 if y0 >= 12 else y1 + 2
    draw.text((x0, text_y), label, fill=color)
