"""The detection pipeline does not care where its numbers come from.

A camera produces a per-segment value by averaging pixels inside an ROI. An electrical
tap on the lamp wiring produces one by reading a GPIO pin: 255 when the lamp lead is
energised, 0 when it is not. Everything downstream -- flash classification, debounce,
state resolution -- sees only the number.

These tests pin that property down, because the whole plan for the GPIO tap rests on
it: the second sensor reuses the engine rather than reimplementing it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stacklight.detector import Detector
from stacklight.sources import create_source
from stacklight.sources.synthetic import ScriptStep, off_step
from stacklight.types import UNKNOWN_STATE, Confidence, SegmentMode

from .conftest import commits

OFF, STEADY, FLASHING = SegmentMode.OFF, SegmentMode.STEADY, SegmentMode.FLASHING
BOTH = ["red", "green"]
WALL0 = datetime(2026, 1, 1, tzinfo=UTC)

#: What an optocoupler output looks like once read as a number.
ENERGISED = 255.0
DARK = 0.0


def drive_pins(config, phases, *, hz=100.0):
    """Feed the detector boolean per-segment samples, as a GPIO tap would.

    ``phases`` is a list of ``(duration_s, fn)`` where ``fn(t)`` returns the lit state
    of each segment at source time ``t``.
    """
    detector = Detector(config)
    readings = []
    dt = 1.0 / hz
    t = 0.0
    seq = 0
    for duration, lit_at in phases:
        for _ in range(round(duration * hz)):
            seq += 1
            values = {name: ENERGISED if lit else DARK for name, lit in lit_at(t).items()}
            readings.append(
                detector.update_values(
                    t=t, wall=WALL0 + timedelta(seconds=t), seq=seq, values=values
                )
            )
            t += dt
    return readings


def all_off(_t):
    return {"red": False, "green": False}


def green_on(_t):
    return {"red": False, "green": True}


def red_flashing(hz=1.5, duty=0.5):
    def lit_at(t):
        return {"red": (t * hz) % 1.0 < duty, "green": False}

    return lit_at


# -- the GPIO path, end to end -----------------------------------------------------


def test_pin_samples_drive_the_full_state_machine(config):
    """off -> steady green -> flashing red -> off, sampled as pin states rather than
    pixels, produces the same committed sequence the camera produces."""
    readings = drive_pins(
        config,
        [(4.0, all_off), (5.0, green_on), (5.0, red_flashing()), (4.0, all_off)],
    )
    assert [state for state, _ in commits(readings)] == [
        "idle",
        "running",
        "attention",
        "idle",
    ]


def test_flash_classification_works_on_pin_samples(config):
    """The duty-cycle machinery is not camera-specific. A pulsed lamp lead reads as
    flashing for exactly the same reasons a blinking LED does."""
    readings = drive_pins(config, [(3.0, all_off), (6.0, red_flashing())])
    settled = [r for r in readings if r.t > 8.0][0]
    assert settled.segments["red"].mode is FLASHING
    assert settled.segments["red"].duty == pytest.approx(0.5, abs=0.05)
    assert settled.segments["red"].transitions >= 2


def test_a_faster_sample_rate_sharpens_the_duty_cycle(config):
    """One concrete gain from the tap: 100Hz polling resolves the flash waveform far
    better than 15fps of video, so duty cycle lands much closer to the truth."""
    at_15 = drive_pins(config, [(3.0, all_off), (6.0, red_flashing())], hz=15.0)
    at_200 = drive_pins(config, [(3.0, all_off), (6.0, red_flashing())], hz=200.0)
    error_15 = abs([r for r in at_15 if r.t > 8.0][0].segments["red"].duty - 0.5)
    error_200 = abs([r for r in at_200 if r.t > 8.0][0].segments["red"].duty - 0.5)
    assert error_200 <= error_15


def test_debounce_still_applies_to_pin_samples(config):
    """A contact bounce or a brief pulse must not commit, same as a flicker of light."""
    readings = drive_pins(config, [(4.0, all_off), (0.2, green_on), (4.0, all_off)])
    assert "running" not in [state for state, _ in commits(readings)]


def test_an_unconfigured_pin_pattern_still_resolves_to_unknown(config):
    """No guessing, whichever sensor produced the pattern."""

    def both_on(_t):
        return {"red": True, "green": True}

    readings = drive_pins(config, [(3.0, all_off), (6.0, both_on)])
    assert [state for state, _ in commits(readings)] == ["idle", UNKNOWN_STATE]


def test_warm_up_applies_to_pin_samples_too(config):
    readings = drive_pins(config, [(1.5, green_on)])
    assert all(not r.changed for r in readings)
    assert readings[-1].confidence is Confidence.DEGRADED
    assert readings[-1].state is None


# -- the two sensors must agree ----------------------------------------------------


def test_camera_and_pin_tap_reach_identical_verdicts(config):
    """The production cross-check, as a test.

    Both detectors watch the same scripted light: one through rendered pixels, the
    other through the synthetic source's own ground truth, which is what a tap on the
    lamp lead would see. Running both nodes side by side is the plan for validating
    the tap on the real machine, and disagreement is the signal that something is
    wrong. They had better agree here.
    """
    script = [
        off_step(4.0, BOTH),
        ScriptStep(5.0, {"green": STEADY}),
        ScriptStep(5.0, {"red": FLASHING}),
        off_step(4.0, BOTH),
    ]
    source = create_source(config, override="synthetic")
    source.set_script(script, loop=False)

    by_camera = Detector(config)
    by_tap = Detector(config)
    camera_readings, tap_readings = [], []

    with source:
        for frame in source.frames(limit=round(18.0 * config.camera.fps)):
            camera_readings.append(by_camera.update(frame))
            # Exactly what an optocoupler on each lamp lead would report.
            tap_readings.append(
                by_tap.update_values(
                    t=frame.t,
                    wall=frame.wall,
                    seq=frame.seq,
                    values={
                        name: ENERGISED if source.is_lit(name, frame.t) else DARK
                        for name in config.segment_names
                    },
                )
            )

    assert commits(camera_readings) == commits(tap_readings)
    assert [state for state, _ in commits(tap_readings)] == [
        "idle",
        "running",
        "attention",
        "idle",
    ]


# -- contract --------------------------------------------------------------------


def test_a_missing_segment_value_is_a_clear_error(config):
    """Better than silently treating an unread pin as dark, which would look exactly
    like a segment that is switched off."""
    detector = Detector(config)
    with pytest.raises(KeyError, match="green"):
        detector.update_values(t=0.0, wall=WALL0, seq=1, values={"red": DARK})


def test_a_sensor_without_a_dark_reference_reports_none(config):
    """A GPIO tap has no dark reference -- there is no frame to put one in."""
    detector = Detector(config)
    reading = detector.update_values(t=0.0, wall=WALL0, seq=1, values={"red": DARK, "green": DARK})
    assert reading.dark_reference is None


def test_the_camera_path_still_goes_through_the_same_core(config):
    """update() is now a thin wrapper, so a camera frame and the equivalent pin
    samples must produce the same segment readings."""
    source = create_source(config, override="synthetic")
    with source:
        source.set_mode("green", STEADY)
        frame = source.read()

    by_camera = Detector(config).update(frame)
    by_values = Detector(config).update_values(
        t=frame.t,
        wall=frame.wall,
        seq=frame.seq,
        values={"red": DARK, "green": ENERGISED},
    )
    assert by_camera.modes == by_values.modes
    assert by_camera.candidate_state == by_values.candidate_state
