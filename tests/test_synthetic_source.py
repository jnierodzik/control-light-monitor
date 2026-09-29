"""Tests for the synthetic frame source.

These assert the properties the detection engine will lean on: lit segments are bright
inside their ROI, everything else stays dark, flashing actually toggles at the
configured rate, and exposure scales brightness the way a sensor would.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from stacklight.clock import VirtualClock
from stacklight.config import Roi, SyntheticSegmentSpec, parse_config
from stacklight.errors import SourceError
from stacklight.sources import CameraControls, SyntheticSource, create_source
from stacklight.sources.synthetic import ScriptStep, off_step
from stacklight.types import SegmentMode

OFF, STEADY, FLASHING = SegmentMode.OFF, SegmentMode.STEADY, SegmentMode.FLASHING

RED_ROI = Roi(20, 10, 20, 20)
GREEN_ROI = Roi(20, 40, 20, 20)
DARK_ROI = Roi(100, 80, 20, 20)


def make_source(**kwargs) -> SyntheticSource:
    defaults = {
        "lit_value": 210.0,
        "noise_sigma": 0.0,
        "ambient": 0.0,
        "glow_px": 0,
        "clock": VirtualClock(),
    }
    defaults.update(kwargs)
    return SyntheticSource(
        (160, 120),
        20.0,
        [
            SyntheticSegmentSpec("red", RED_ROI, (255, 0, 0)),
            SyntheticSegmentSpec("green", GREEN_ROI, (0, 255, 0)),
        ],
        controls=CameraControls(exposure_us=800, analogue_gain=1.0),
        **defaults,
    )


def roi_value(frame, roi: Roi) -> float:
    """Max-channel mean over an ROI -- the metric the detector defaults to."""
    rows, cols = roi.slices
    return float(frame.data[rows, cols].max(axis=2).mean())


# -- frame shape and contract ------------------------------------------------------


def test_frames_are_uint8_rgb_at_the_configured_resolution():
    with make_source() as source:
        frame = source.read()
    assert frame.data.shape == (120, 160, 3)
    assert frame.data.dtype == np.uint8
    assert frame.resolution == (160, 120)


def test_read_before_start_is_an_error():
    source = make_source()
    with pytest.raises(SourceError, match="before start"):
        source.read()


def test_sequence_numbers_and_timestamps_advance_at_the_frame_rate():
    with make_source() as source:
        frames = list(source.frames(limit=5))
    assert [f.seq for f in frames] == [1, 2, 3, 4, 5]
    deltas = np.diff([f.t for f in frames])
    assert deltas == pytest.approx(1 / 20.0)


def test_virtual_clock_makes_replay_instant():
    clock = VirtualClock()
    with make_source(clock=clock) as source:
        list(source.frames(limit=200))
    # 200 frames at 20fps is ten seconds of source time, taken in no real time at all.
    assert clock.now() == pytest.approx(200 / 20.0, abs=1 / 20.0)


# -- segments render where and only where they should ------------------------------


def test_off_segments_leave_the_frame_black():
    with make_source() as source:
        frame = source.read()
    assert frame.data.max() == 0


def test_a_steady_segment_is_bright_inside_its_roi_and_nowhere_else():
    with make_source() as source:
        source.set_mode("red", STEADY)
        frame = source.read()

    assert roi_value(frame, RED_ROI) == pytest.approx(210, abs=1)
    assert roi_value(frame, GREEN_ROI) == 0
    assert roi_value(frame, DARK_ROI) == 0


def test_segments_are_independent():
    with make_source() as source:
        source.set_modes({"red": STEADY, "green": STEADY})
        frame = source.read()
    assert roi_value(frame, RED_ROI) == pytest.approx(210, abs=1)
    assert roi_value(frame, GREEN_ROI) == pytest.approx(210, abs=1)


def test_position_not_hue_identifies_a_segment():
    """A red segment rendered green still reads as lit -- that is the point of using
    the max channel and fixed rectangles rather than classifying colour."""
    with make_source() as source:
        source.set_mode("red", STEADY)
        frame = source.read()
    rows, cols = RED_ROI.slices
    patch = frame.data[rows, cols]
    assert patch[..., 0].mean() > 200  # red channel carries it
    assert patch[..., 1].mean() < 5


def test_glow_bleeds_past_the_roi_edge():
    with make_source(glow_px=6) as source:
        source.set_mode("red", STEADY)
        frame = source.read()
    just_outside = Roi(RED_ROI.x, RED_ROI.y - 5, RED_ROI.w, 4)
    assert 0 < roi_value(frame, just_outside) < 210


def test_glow_does_not_brighten_the_segment_it_surrounds():
    """The halo must not stack on top of the core, or a lit segment clips at 255 and
    lit_value stops meaning what it says."""
    with make_source(glow_px=6) as source:
        source.set_mode("red", STEADY)
        assert roi_value(source.read(), RED_ROI) == pytest.approx(210, abs=1)


def test_ambient_and_noise_lift_the_floor_without_reaching_threshold():
    with make_source(ambient=6.0, noise_sigma=1.5) as source:
        frame = source.read()
    floor = roi_value(frame, DARK_ROI)
    assert 3 < floor < 20  # visible, but nowhere near a threshold of 60 or 90


def test_unknown_segment_name_is_rejected():
    with make_source() as source, pytest.raises(KeyError, match="amber"):
        source.set_mode("amber", STEADY)


def test_duplicate_synthetic_segments_are_rejected():
    with pytest.raises(ValueError, match="duplicate synthetic segment"):
        SyntheticSource(
            (64, 48),
            10,
            [SyntheticSegmentSpec("red", Roi(0, 0, 8, 8), (255, 0, 0))] * 2,
        )


def test_segment_outside_the_frame_is_rejected():
    with pytest.raises(Exception, match="extends past"):
        SyntheticSource(
            (64, 48),
            10,
            [SyntheticSegmentSpec("red", Roi(60, 0, 8, 8), (255, 0, 0))],
        )


# -- flashing ----------------------------------------------------------------------


def test_flashing_toggles_and_holds_the_configured_duty_cycle():
    with make_source(flash_hz=2.0, flash_duty=0.5) as source:
        source.set_mode("red", FLASHING)
        values = [roi_value(f, RED_ROI) for f in source.frames(limit=40)]

    lit = [v > 90 for v in values]
    assert any(lit) and not all(lit)
    assert sum(lit) / len(lit) == pytest.approx(0.5, abs=0.1)
    transitions = sum(a != b for a, b in pairwise(lit))
    assert transitions >= 4  # 2 Hz over 2 seconds


def test_flash_phase_starts_lit_so_tests_are_deterministic():
    with make_source(flash_hz=1.0, flash_duty=0.5) as source:
        source.read()  # advance off the zero mark
        source.set_mode("red", FLASHING)
        assert roi_value(source.read(), RED_ROI) > 90


@pytest.mark.parametrize("duty", [0.1, 0.5, 0.9])
def test_duty_cycle_is_honoured_across_the_range(duty):
    with make_source(flash_hz=1.0, flash_duty=duty) as source:
        source.set_mode("red", FLASHING)
        values = [roi_value(f, RED_ROI) for f in source.frames(limit=80)]
    assert sum(v > 90 for v in values) / len(values) == pytest.approx(duty, abs=0.08)


def test_per_segment_flash_overrides_beat_the_source_default():
    with make_source(flash_hz=1.0, flash_duty=0.5) as source:
        source.set_mode("red", FLASHING, flash_hz=1.0, flash_duty=0.05)
        values = [roi_value(f, RED_ROI) for f in source.frames(limit=60)]
    # A 5% duty cycle must not look like a 50% one; the detector has to tell them apart.
    assert sum(v > 90 for v in values) / len(values) < 0.15


# -- scripting ---------------------------------------------------------------------


def test_a_script_walks_the_pole_through_a_sequence():
    """off -> steady green -> flashing red -> off, the canonical sequence the state
    machine tests will drive."""
    source = make_source(flash_hz=2.0, flash_duty=0.5)
    source.set_script(
        [
            off_step(1.0, ["red", "green"]),
            ScriptStep(1.0, {"green": STEADY}),
            ScriptStep(2.0, {"red": FLASHING}),
            off_step(1.0, ["red", "green"]),
        ],
        loop=False,
    )

    phases: list[tuple[float, float, float]] = []
    with source:
        for frame in source.frames(limit=100):  # 5 seconds at 20fps
            phases.append((frame.t, roi_value(frame, RED_ROI), roi_value(frame, GREEN_ROI)))

    def window(lo, hi):
        return [(r, g) for t, r, g in phases if lo <= t < hi]

    assert all(r < 5 and g < 5 for r, g in window(0.0, 1.0))
    assert all(r < 5 and g > 90 for r, g in window(1.0, 2.0))
    red_flash = [r for r, _ in window(2.0, 4.0)]
    assert any(r > 90 for r in red_flash) and any(r < 5 for r in red_flash)
    assert all(g < 5 for _, g in window(2.0, 4.0))
    assert all(r < 5 and g < 5 for r, g in window(4.0, 5.0))


def test_a_script_loops_by_default():
    source = make_source()
    source.set_script([ScriptStep(0.5, {"green": STEADY}), off_step(0.5, ["green"])])
    with source:
        values = [roi_value(f, GREEN_ROI) for f in source.frames(limit=60)]  # 3 seconds
    lit = [v > 90 for v in values]
    assert sum(a != b for a, b in pairwise(lit)) >= 4  # cycled at least three times


def test_a_non_looping_script_holds_its_last_step():
    source = make_source()
    source.set_script([ScriptStep(0.5, {"green": STEADY})], loop=False)
    with source:
        list(source.frames(limit=20))  # one second, well past the 0.5s script
        assert source.script_finished
        assert roi_value(source.read(), GREEN_ROI) > 90


def test_script_referencing_an_unknown_segment_is_rejected():
    source = make_source()
    with pytest.raises(KeyError, match="amber"):
        source.set_script([ScriptStep(1.0, {"amber": STEADY})])


def test_segments_omitted_from_a_step_are_turned_off():
    source = make_source()
    source.set_script([ScriptStep(5.0, {"red": STEADY})], loop=False)
    with source:
        source.set_mode("green", STEADY)
        frame = source.read()
    assert roi_value(frame, GREEN_ROI) == 0


# -- camera controls ---------------------------------------------------------------


def test_exposure_and_gain_scale_brightness_like_a_sensor():
    with make_source() as source:
        source.set_mode("red", STEADY)
        assert roi_value(source.read(), RED_ROI) == pytest.approx(210, abs=1)

        source.set_controls(exposure_us=400)  # half the light
        assert roi_value(source.read(), RED_ROI) == pytest.approx(105, abs=2)

        source.set_controls(analogue_gain=4.0)  # gain multiplies the same way
        assert roi_value(source.read(), RED_ROI) == pytest.approx(255, abs=1)  # clipped


def test_underexposure_drops_a_lit_segment_below_threshold():
    """The failure mode the calibration UI exists to prevent: dial exposure too low and
    a genuinely lit segment stops registering."""
    with make_source() as source:
        source.set_mode("red", STEADY)
        source.set_controls(exposure_us=100)
        assert roi_value(source.read(), RED_ROI) < 90


def test_controls_round_trip():
    source = make_source()
    controls = source.set_controls(exposure_us=1200, analogue_gain=2.0, lens_position=3.5)
    assert controls.exposure_us == 1200
    assert controls.analogue_gain == 2.0
    assert controls.lens_position == 3.5
    assert controls.awb_enable is False
    assert source.controls == controls


# -- construction from config ------------------------------------------------------


def test_synthetic_source_mirrors_the_detection_rois(config):
    """A real node config replays on a laptop with no extra setup: the synthetic
    source borrows the detection ROIs and colours."""
    source = create_source(config)
    assert isinstance(source, SyntheticSource)
    assert source.segment_names == ("red", "green")
    with source:
        source.set_mode("red", STEADY)
        frame = source.read()
    assert roi_value(frame, config.segment("red").roi) > config.segment("red").threshold
    assert roi_value(frame, config.segment("green").roi) < config.segment("green").threshold


def test_explicit_synthetic_segments_override_the_mirror(config_dict):
    config_dict["camera"]["synthetic"]["segments"] = [
        {"name": "red", "roi": [0, 0, 10, 10], "color": [255, 0, 0]},
    ]
    source = create_source(parse_config(config_dict))
    assert source.segment_names == ("red",)


def test_source_override_swaps_the_backend_without_editing_the_file(config_dict):
    config_dict["camera"]["source"] = "picamera"
    source = create_source(parse_config(config_dict), override="synthetic")
    assert isinstance(source, SyntheticSource)


def test_picamera_source_fails_clearly_on_a_machine_without_it(config_dict, monkeypatch):
    """Constructing is fine anywhere; it is starting that needs the hardware. The error
    has to name the fix, because a bare ImportError on picamera2 is a puzzle."""
    import sys

    from stacklight.errors import SourceError

    monkeypatch.setitem(sys.modules, "picamera2", None)
    config_dict["camera"]["source"] = "picamera"
    source = create_source(parse_config(config_dict))
    with pytest.raises(SourceError, match="system-site-packages"):
        source.start()
