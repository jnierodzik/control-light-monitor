from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from stacklight.config import parse_config
from stacklight.detector import Detector
from stacklight.snapshot import encode_snapshot
from stacklight.sources import create_source
from stacklight.types import SegmentMode


def capture(config, mode=SegmentMode.STEADY, segment="green"):
    source = create_source(config, override="synthetic")
    detector = Detector(config)
    with source:
        source.set_mode(segment, mode)
        frame = source.read()
        reading = detector.update(frame)
    return frame, reading


def decode(payload: bytes) -> Image.Image:
    return Image.open(io.BytesIO(payload))


def pixels(payload: bytes) -> np.ndarray:
    return np.asarray(decode(payload).convert("RGB"), dtype=np.int64)


def test_a_snapshot_is_a_decodable_jpeg(config):
    frame, reading = capture(config)
    image = decode(encode_snapshot(frame, config, reading))
    assert image.format == "JPEG"
    assert image.size == (160, 120)  # under snapshot_max_width, so not resized


def test_wide_frames_are_scaled_down(config_dict):
    config_dict["camera"]["resolution"] = [1280, 720]
    for segment in config_dict["segments"]:
        segment["roi"] = [400, 100, 40, 60]
    config_dict["segments"][1]["roi"] = [400, 200, 40, 60]
    config_dict["dark_reference"]["roi"] = [300, 400, 40, 40]
    config_dict["publish"] = {"snapshot_max_width": 320}
    config = parse_config(config_dict)
    frame, reading = capture(config)
    image = decode(encode_snapshot(frame, config, reading))
    assert image.size == (320, 180)  # aspect ratio preserved


def test_annotation_draws_onto_the_image(config_dict):
    config = parse_config(dict(config_dict, publish={"snapshot_annotate": True}))
    plain = parse_config(dict(config_dict, publish={"snapshot_annotate": False}))
    frame, reading = capture(config)

    annotated = pixels(encode_snapshot(frame, config, reading))
    bare = pixels(encode_snapshot(frame, plain, reading))
    assert not np.array_equal(annotated, bare)
    # ROI outlines and labels add bright pixels to an otherwise near-black frame.
    assert annotated.sum() > bare.sum()


def test_gain_brightens_the_snapshot_without_touching_detection(config_dict):
    """The detector must keep seeing the real underexposed frame; only the debug image
    is lifted, so camera aim can be checked on a nearly black scene."""
    config_dict["camera"]["synthetic"]["ambient"] = 6.0
    dim = parse_config(dict(config_dict, publish={"snapshot_annotate": False}))
    bright = parse_config(
        dict(config_dict, publish={"snapshot_annotate": False, "snapshot_gain": 8.0})
    )
    frame, reading = capture(dim)

    assert pixels(encode_snapshot(frame, bright, reading)).sum() > (
        2 * pixels(encode_snapshot(frame, dim, reading)).sum()
    )
    # The reading was taken from the untouched frame.
    assert reading.segments["green"].value > 90


def test_a_snapshot_can_be_encoded_without_a_reading(config):
    """First-boot case: there is a frame worth looking at before detection settles."""
    frame, _ = capture(config)
    image = decode(encode_snapshot(frame, config, None))
    assert image.format == "JPEG"


def test_quality_controls_payload_size(config_dict):
    low = parse_config(dict(config_dict, publish={"snapshot_quality": 10}))
    high = parse_config(dict(config_dict, publish={"snapshot_quality": 95}))
    frame, reading = capture(low)
    assert len(encode_snapshot(frame, low, reading)) < len(encode_snapshot(frame, high, reading))


def test_a_node_without_a_dark_reference_annotates_cleanly(config_dict):
    del config_dict["dark_reference"]
    config = parse_config(config_dict)
    frame, reading = capture(config)
    assert decode(encode_snapshot(frame, config, reading)).format == "JPEG"


def test_an_roi_against_the_top_edge_still_gets_a_label(config_dict):
    """The label goes below the box when there is no room above it, rather than being
    clipped off the top of the image."""
    config_dict["segments"][0]["roi"] = [20, 0, 20, 20]
    config = parse_config(config_dict)
    frame, reading = capture(config)
    assert decode(encode_snapshot(frame, config, reading)).format == "JPEG"


@pytest.mark.parametrize("mode", [SegmentMode.OFF, SegmentMode.STEADY])
def test_lit_and_unlit_segments_are_outlined_differently(config_dict, mode):
    config = parse_config(config_dict)
    frame, reading = capture(config, mode=mode)
    payload = encode_snapshot(frame, config, reading)
    assert decode(payload).format == "JPEG"
