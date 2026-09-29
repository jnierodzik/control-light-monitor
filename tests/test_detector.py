"""Detection engine tests, driven end to end through SyntheticSource.

Every test here goes frames -> ROI sampling -> mode classification -> state resolution
-> debounce, on a virtual clock. No camera, no broker, no sleeping.
"""

from __future__ import annotations

import time

import pytest

from stacklight.config import parse_config
from stacklight.detector import (
    REASON_DARK_REFERENCE,
    REASON_WARMING_UP,
    Detector,
    detection_latency_bounds,
)
from stacklight.sources import create_source
from stacklight.sources.synthetic import ScriptStep, off_step
from stacklight.types import UNKNOWN_STATE, Confidence, SegmentMode

from .conftest import at, commits, run_script, states

OFF, STEADY, FLASHING = SegmentMode.OFF, SegmentMode.STEADY, SegmentMode.FLASHING
BOTH = ["red", "green"]


# -- the canonical sequence --------------------------------------------------------


def test_the_canonical_sequence_commits_the_expected_states(config):
    """off -> steady green -> flashing red -> off  ==>  idle -> running -> attention -> idle."""
    readings = run_script(
        config,
        [
            off_step(3.0, BOTH),
            ScriptStep(4.0, {"green": STEADY}),
            ScriptStep(4.0, {"red": FLASHING}),
            off_step(3.0, BOTH),
        ],
    )
    assert states(readings) == ["idle", "running", "attention", "idle"]


def test_commit_delay_matches_the_predicted_latency(config):
    """Each commit lands inside the window the config implies, so a tuning change has
    a predictable effect rather than a mysterious one."""
    best, worst = detection_latency_bounds(config.detection)
    readings = run_script(
        config,
        [
            off_step(3.0, BOTH),
            ScriptStep(4.0, {"green": STEADY}),
            ScriptStep(4.0, {"red": FLASHING}),
            off_step(3.0, BOTH),
        ],
    )
    committed = commits(readings)
    # Duty cycle is quantised to whole frames, so allow a couple of frames of slack.
    # Rounding keeps float noise from making an exact boundary look like a failure.
    slack = 2.0 / config.camera.fps
    # The first commit also waits out the warm-up window; the rest are steady state.
    for (_, at_t), changed_at in zip(committed[1:], [3.0, 7.0, 11.0], strict=True):
        assert at_t >= round(changed_at + best - slack, 6)
        assert at_t <= round(changed_at + worst + slack, 6)


def test_flashing_is_never_collapsed_into_steady(config):
    """Stack lights use flash to mean something different from solid on, so a flashing
    red must never be reported as the steady-red 'fault' state."""
    readings = run_script(config, [off_step(3.0, BOTH), ScriptStep(6.0, {"red": FLASHING})])
    assert states(readings) == ["idle", "attention"]
    assert "fault" not in [r.candidate_state for r in readings]
    assert at(readings, 8.5).segments["red"].mode is FLASHING


def test_a_segment_going_from_flashing_to_steady_is_a_transition(config):
    readings = run_script(
        config,
        [
            off_step(3.0, BOTH),
            ScriptStep(5.0, {"red": FLASHING}),
            ScriptStep(5.0, {"red": STEADY}),
        ],
    )
    assert states(readings) == ["idle", "attention", "fault"]


# -- debounce ----------------------------------------------------------------------


def test_a_blip_never_commits_the_lit_state(config):
    """A half-second flicker of green must not produce a 'running' notification."""
    readings = run_script(
        config,
        [off_step(4.0, BOTH), ScriptStep(0.5, {"green": STEADY}), off_step(4.0, BOTH)],
    )
    assert "running" not in states(readings)


def test_a_single_blink_is_indistinguishable_from_a_flash(config):
    """A consequence of flash_min_transitions=2 worth knowing about.

    One 0.5s pulse puts two transitions and a 24% duty cycle in a 2s window, which is
    exactly what flashing looks like, and it holds that way for longer than the 1s
    debounce. So the pulse commits: green flashing matches no configured state, so it
    lands on 'unknown' rather than on 'running'.

    Raising flash_min_transitions to 3 rejects single pulses, at the cost of needing
    roughly 0.75Hz or faster to recognise a real flash in a 2s window.
    """
    readings = run_script(
        config,
        [off_step(4.0, BOTH), ScriptStep(0.5, {"green": STEADY}), off_step(4.0, BOTH)],
    )
    assert states(readings) == ["idle", UNKNOWN_STATE, "idle"]
    assert at(readings, 5.5).segments["green"].mode is FLASHING


def test_a_state_must_hold_continuously_not_cumulatively(fast_window_config_dict):
    """Debounce is a continuous-hold requirement, not a running total.

    Green spends six seconds lit across these steps, but never two seconds at a stretch,
    so 'running' is never committed.
    """
    config = parse_config(fast_window_config_dict)
    readings = run_script(
        config,
        [off_step(4.0, BOTH)] + [ScriptStep(1.0, {"green": STEADY}), off_step(1.0, BOTH)] * 6,
    )
    assert states(readings) == ["idle"]
    # The segment itself was clearly seen going on and off; it is the state that never settled.
    assert at(readings, 4.6).segments["green"].mode is STEADY
    assert at(readings, 4.6).candidate_state == "running"


def test_a_pattern_that_outlasts_the_debounce_does_commit(fast_window_config_dict):
    """The other half of the previous test: same config, one long hold, and it lands."""
    config = parse_config(fast_window_config_dict)
    readings = run_script(config, [off_step(4.0, BOTH), ScriptStep(4.0, {"green": STEADY})])
    assert states(readings) == ["idle", "running"]


def test_zero_debounce_commits_as_soon_as_the_pattern_resolves(config_dict):
    config_dict["detection"]["debounce_s"] = 0.0
    config = parse_config(config_dict)
    readings = run_script(config, [off_step(4.0, BOTH)])
    first = commits(readings)[0]
    assert first[0] == "idle"
    assert first[1] == pytest.approx(config.detection.flash_window_s, abs=0.1)


def test_since_marks_when_the_pattern_appeared_not_when_debounce_expired(config):
    readings = run_script(config, [off_step(3.0, BOTH), ScriptStep(5.0, {"green": STEADY})])
    commit = next(r for r in readings if r.changed and r.state == "running")
    assert commit.since_t is not None
    # The pattern was recognised a full debounce period before it was committed.
    assert commit.t - commit.since_t == pytest.approx(config.detection.debounce_s, abs=0.1)
    assert commit.since == at(readings, commit.since_t).wall


# -- flash classification edges ----------------------------------------------------


def test_a_very_short_blink_reads_off_not_flashing(config):
    """A 5% duty cycle falls outside flash_duty_range, so by the configured definition
    it is not flashing. It reads off -- worth knowing when tuning the range."""
    readings = run_script(
        config,
        [
            off_step(3.0, BOTH),
            ScriptStep(6.0, {"red": FLASHING}, flash_hz=1.5, flash_duty=0.05),
        ],
    )
    assert at(readings, 8.5).segments["red"].mode is OFF
    assert states(readings) == ["idle"]


def test_a_near_continuous_blink_reads_steady(config):
    """A 95% duty cycle is above flash_duty_range, so it resolves to steady."""
    readings = run_script(
        config,
        [
            off_step(3.0, BOTH),
            ScriptStep(6.0, {"red": FLASHING}, flash_hz=1.5, flash_duty=0.95),
        ],
    )
    assert at(readings, 8.5).segments["red"].mode is STEADY
    assert states(readings) == ["idle", "fault"]


def test_a_single_transition_does_not_qualify_as_flashing(config):
    """flash_min_transitions exists so one on-edge inside the window is not a flash."""
    readings = run_script(config, [off_step(3.0, BOTH), ScriptStep(5.0, {"green": STEADY})])
    green = at(readings, 4.0).segments["green"]
    assert green.transitions <= 1
    assert green.mode is not FLASHING


@pytest.mark.parametrize("hz", [0.8, 1.5, 4.0])
def test_flash_is_detected_across_a_range_of_rates(config, hz):
    readings = run_script(
        config,
        [off_step(3.0, BOTH), ScriptStep(6.0, {"red": FLASHING}, flash_hz=hz, flash_duty=0.5)],
    )
    assert at(readings, 8.5).segments["red"].mode is FLASHING
    assert "attention" in states(readings)


def test_a_flash_slower_than_the_window_cannot_be_seen(config):
    """0.2 Hz is one edge per 2.5s against a 2s window: physically undetectable as a
    flash, and it reads as alternating steady/off instead. Widen flash_window_s if the
    real light turns out to blink this slowly."""
    readings = run_script(
        config,
        [off_step(3.0, BOTH), ScriptStep(20.0, {"red": FLASHING}, flash_hz=0.2, flash_duty=0.5)],
    )
    observed = {r.segments["red"].mode for r in readings if r.t > 6.0}
    assert FLASHING not in observed
    assert observed == {OFF, STEADY}


# -- unmatched patterns ------------------------------------------------------------


def test_an_unconfigured_pattern_commits_unknown_rather_than_a_guess(config):
    """Both segments solid matches nothing in the config, and must not be forced into
    the nearest named state."""
    readings = run_script(
        config,
        [off_step(3.0, BOTH), ScriptStep(6.0, {"red": STEADY, "green": STEADY})],
    )
    assert states(readings) == ["idle", UNKNOWN_STATE]
    # Confidence stays OK: the reading is trustworthy, it is the meaning that is unknown.
    assert at(readings, 8.5).confidence is Confidence.OK


def test_wildcard_state_absorbs_whatever_the_other_segment_does(config):
    """`attention: {red: flashing}` must match with green off and with green on."""
    readings = run_script(
        config,
        [
            off_step(3.0, BOTH),
            ScriptStep(5.0, {"red": FLASHING}),
            ScriptStep(5.0, {"red": FLASHING, "green": STEADY}),
        ],
    )
    assert states(readings) == ["idle", "attention"]  # no second commit; still attention
    assert at(readings, 12.0).segments["green"].mode is STEADY


# -- warm-up -----------------------------------------------------------------------


def test_nothing_commits_while_the_window_is_filling(config):
    readings = run_script(config, [ScriptStep(6.0, {"green": STEADY})])
    for reading in readings:
        if reading.t < config.detection.flash_window_s:
            assert not reading.changed
            assert reading.confidence is Confidence.DEGRADED
            assert REASON_WARMING_UP in reading.reasons
            assert reading.state is None


def test_boot_does_not_publish_a_spurious_unknown(config):
    """During warm-up the candidate is 'unknown', and with a 1s debounce against a 2s
    window that would otherwise commit and notify. Startup is not news."""
    readings = run_script(config, [ScriptStep(6.0, {"green": STEADY})])
    assert states(readings) == ["running"]


def test_confidence_clears_once_the_window_is_full(config):
    readings = run_script(config, [ScriptStep(6.0, {"green": STEADY})])
    settled = at(readings, 5.0)
    assert settled.confidence is Confidence.OK
    assert settled.reasons == ()
    assert all(r.settled for r in settled.segments.values())


# -- dark reference ----------------------------------------------------------------


def test_stray_light_on_the_dark_reference_degrades_to_unknown(dark_ref_config_dict):
    """A region that should never be lit going bright means the camera has been bumped
    or the lighting changed. The answer becomes 'unknown', not a confident guess."""
    config = parse_config(dark_ref_config_dict)
    readings = run_script(
        config,
        [
            ScriptStep(5.0, {"green": STEADY}),
            ScriptStep(6.0, {"green": STEADY, "stray_light": STEADY}),
        ],
    )
    assert states(readings) == ["running", UNKNOWN_STATE]
    degraded = at(readings, 10.0)
    assert degraded.confidence is Confidence.DEGRADED
    assert REASON_DARK_REFERENCE in degraded.reasons
    assert degraded.dark_reference is not None
    assert degraded.dark_reference.value > config.dark_reference.threshold
    # The segments themselves still read fine -- the raw values stay available for
    # diagnosis even though the verdict is withheld.
    assert degraded.segments["green"].mode is STEADY


def test_the_node_recovers_when_the_dark_reference_goes_dark_again(dark_ref_config_dict):
    config = parse_config(dark_ref_config_dict)
    readings = run_script(
        config,
        [
            ScriptStep(5.0, {"green": STEADY}),
            ScriptStep(6.0, {"green": STEADY, "stray_light": STEADY}),
            ScriptStep(6.0, {"green": STEADY}),
        ],
    )
    assert states(readings) == ["running", UNKNOWN_STATE, "running"]
    assert at(readings, 16.0).confidence is Confidence.OK


def test_a_momentary_flash_does_not_trip_the_dark_reference(dark_ref_config_dict):
    """A reflection or someone's torch passing through frame is not a bumped camera."""
    config = parse_config(dark_ref_config_dict)
    readings = run_script(
        config,
        [
            ScriptStep(5.0, {"green": STEADY}),
            ScriptStep(0.2, {"green": STEADY, "stray_light": STEADY}),
            ScriptStep(6.0, {"green": STEADY}),
        ],
    )
    assert states(readings) == ["running"]
    assert all(REASON_DARK_REFERENCE not in r.reasons for r in readings)


def test_a_node_without_a_dark_reference_still_works(config_dict):
    del config_dict["dark_reference"]
    readings = run_script(parse_config(config_dict), [ScriptStep(6.0, {"green": STEADY})])
    assert states(readings) == ["running"]
    assert at(readings, 5.0).dark_reference is None


# -- raw values, for tuning --------------------------------------------------------


def test_raw_brightness_values_are_carried_in_every_reading(config):
    readings = run_script(config, [off_step(3.0, BOTH), ScriptStep(4.0, {"green": STEADY})])
    lit = at(readings, 6.0)
    assert lit.values["green"] > config.segment("green").threshold
    assert lit.values["red"] < config.segment("red").threshold
    assert lit.segments["green"].lit is True
    assert lit.segments["green"].duty == pytest.approx(1.0)
    assert lit.segments["green"].threshold == 90


def test_the_measured_frame_rate_is_reported(config):
    readings = run_script(config, [ScriptStep(4.0, {"green": STEADY})])
    assert at(readings, 3.0).fps == pytest.approx(config.camera.fps, rel=0.05)


# -- the metric matters ------------------------------------------------------------


def test_mean_rgb_loses_a_lit_red_segment_that_max_channel_sees(config_dict):
    """The concrete reason 'value' is the default: with a plain RGB mean, a fully lit
    red segment reads ~85 against a threshold of 90 and the fault state is missed."""
    config_dict["segments"][0]["color"] = [255, 0, 0]
    script = [off_step(3.0, BOTH), ScriptStep(5.0, {"red": STEADY})]

    config_dict["detection"]["metric"] = "value"
    with_value = run_script(parse_config(config_dict), script)
    assert states(with_value) == ["idle", "fault"]

    config_dict["detection"]["metric"] = "mean_rgb"
    with_mean = run_script(parse_config(config_dict), script)
    assert states(with_mean) == ["idle"]
    assert at(with_mean, 7.0).values["red"] < 90


def test_underexposure_makes_a_lit_segment_disappear(config):
    """What a badly calibrated exposure looks like from the engine's side."""
    source = create_source(config, override="synthetic")
    detector = Detector(config)
    with source:
        source.set_mode("green", STEADY)
        source.set_controls(exposure_us=100)
        readings = [detector.update(frame) for frame in source.frames(limit=100)]
    assert states(readings) == ["idle"]  # the machine is running, the node cannot tell


# -- engine mechanics --------------------------------------------------------------


def test_the_engine_never_reads_the_wall_clock(config):
    """A minute of light behaviour must cost no real time. If any timing came from
    time.monotonic() instead of Frame.t, this would take a minute."""
    started = time.monotonic()
    readings = run_script(
        config,
        [off_step(20.0, BOTH), ScriptStep(20.0, {"green": STEADY}), off_step(20.0, BOTH)],
    )
    assert time.monotonic() - started < 5.0
    assert readings[-1].t == pytest.approx(60.0, abs=0.1)
    assert states(readings) == ["idle", "running", "idle"]


def test_changed_is_true_on_exactly_one_frame_per_transition(config):
    readings = run_script(config, [off_step(4.0, BOTH), ScriptStep(5.0, {"green": STEADY})])
    assert sum(r.changed for r in readings) == 2
    for reading in readings:
        if not reading.changed:
            continue
        assert reading.state == reading.candidate_state


def test_reset_returns_the_engine_to_a_cold_start(config):
    source = create_source(config, override="synthetic")
    detector = Detector(config)
    with source:
        source.set_mode("green", STEADY)
        for frame in source.frames(limit=100):
            detector.update(frame)
        assert detector.state == "running"

        detector.reset()
        assert detector.state is None
        first = detector.update(source.read())
        assert first.state is None
        assert REASON_WARMING_UP in first.reasons


def test_latency_bounds_reflect_the_config(config_dict):
    config_dict["detection"].update(
        {"debounce_s": 2.0, "flash_window_s": 5.0, "flash_duty_range": [0.25, 0.75]}
    )
    best, worst = detection_latency_bounds(parse_config(config_dict).detection)
    assert best == pytest.approx(5.0 * 0.25 + 2.0)
    assert worst == pytest.approx(5.0 * 0.75 + 2.0)
