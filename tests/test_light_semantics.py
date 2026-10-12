"""What the machine's light means, and how the config encodes it.

Observed on the machine:

    green steady    a cycle is RUNNING
    green flashing  the cycle COMPLETED, waiting to be dismissed
    red lit         a fault, latched until the operator acts
    both dark       off, or idle between runs

Red is the part that behaves like an annunciator: it latches and means the same thing
solid or blinking, which is why states match `on` for it rather than a specific mode.
Green is not -- steady and flashing mean different things, and the whole value of the
tap rests on telling them apart.

This file was originally written on the assumption that the light was a pure
annunciator and that running was unobservable. That was wrong, and expensive: both
green modes resolved to `done`, so the completion alert fired when a cycle STARTED and
nothing fired when it finished. These tests now pin the corrected meaning, from token
parsing through to committed state over both sensors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stacklight.clock import VirtualClock
from stacklight.config import MODE_TOKENS, load_config, parse_config
from stacklight.detector import Detector
from stacklight.errors import ConfigError
from stacklight.sources import create_source
from stacklight.sources.synthetic import ScriptStep, off_step
from stacklight.types import SegmentMode

from .conftest import CONFIG_DIR, commits

OFF, STEADY, FLASHING = SegmentMode.OFF, SegmentMode.STEADY, SegmentMode.FLASHING
W0 = datetime(2026, 1, 1, tzinfo=UTC)
LIT, DARK = 255.0, 0.0


# -- the `on` token ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "mode", "expected"),
    [
        ("on", STEADY, True),
        ("on", FLASHING, True),
        ("on", OFF, False),
        ("steady", STEADY, True),
        ("steady", FLASHING, False),
        ("off", OFF, True),
        ("flashing", FLASHING, True),
    ],
)
def test_mode_tokens_match_the_right_modes(token, mode, expected):
    assert MODE_TOKENS[token].matches(mode) is expected


def test_on_is_how_a_state_stops_caring_whether_the_light_blinks(config_dict):
    config_dict["states"] = {"lit": {"red": "on"}, "dark": {"red": "off"}}
    config = parse_config(config_dict)
    for mode in (STEADY, FLASHING):
        assert config.resolve_state({"red": mode, "green": OFF}) == "lit"
    assert config.resolve_state({"red": OFF, "green": OFF}) == "dark"


def test_unquoted_on_is_caught_with_a_useful_message(tmp_path):
    """YAML 1.1 reads bare on/off as booleans. The error has to say so, or this costs
    somebody twenty minutes."""
    path = tmp_path / "node.yaml"
    path.write_text(
        "node_id: t\n"
        "camera: {source: synthetic, resolution: [64, 48], fps: 10, exposure_us: 800,"
        " analogue_gain: 1.0}\n"
        "segments:\n"
        "  - {name: red, roi: [0, 0, 8, 8], threshold: 90}\n"
        "states:\n"
        "  error: {red: on}\n"
    )
    with pytest.raises(ConfigError, match="YAML reads unquoted on/off as booleans"):
        load_config(path)


def test_an_unknown_mode_token_is_rejected(config_dict):
    config_dict["states"] = {"lit": {"red": "illuminated"}}
    with pytest.raises(ConfigError, match="expected one of"):
        parse_config(config_dict)


# -- the shipped annunciator config ------------------------------------------------


@pytest.mark.parametrize("name", ["machine1.yaml", "dev-synthetic.yaml"])
def test_every_observable_pattern_is_named(name):
    """No pattern falls through to `unknown` any more. With only two segments and an
    annunciator's semantics, there is nothing left to be uncertain about."""
    config = load_config(CONFIG_DIR / name)
    for red in SegmentMode:
        for green in SegmentMode:
            assert config.resolve_state({"red": red, "green": green}) != "unknown"


@pytest.mark.parametrize(
    ("red", "green", "expected"),
    [
        (OFF, OFF, "clear"),
        (OFF, STEADY, "running"),
        (OFF, FLASHING, "done"),
        (STEADY, OFF, "error"),
        (FLASHING, OFF, "error"),
        (STEADY, STEADY, "error"),
        (FLASHING, FLASHING, "error"),
    ],
)
def test_resolution_table(red, green, expected):
    config = load_config(CONFIG_DIR / "machine1.yaml")
    assert config.resolve_state({"red": red, "green": green}) == expected


def test_error_wins_when_both_are_lit(config=None):
    """Encoded structurally rather than by rule order: `error` constrains only red,
    `done` requires red off, so there is no tie for the ambiguity checker to reject."""
    config = load_config(CONFIG_DIR / "machine1.yaml")
    assert config.resolve_state({"red": STEADY, "green": STEADY}) == "error"


def test_clear_does_not_claim_the_machine_is_idle():
    """`clear` means nothing is pending. The machine may be powered off or idle between
    runs, and the light cannot tell those apart -- the name must not imply otherwise.
    Mid-run is no longer in that set: a running cycle holds green steady."""
    config = load_config(CONFIG_DIR / "machine1.yaml")
    assert "idle" not in [spec.name for spec in config.states]
    assert config.resolve_state({"red": OFF, "green": OFF}) == "clear"


# -- behaviour, through the detector -----------------------------------------------


def drive(config, phases, *, hz=100.0):
    """Feed latched-light samples through the detector, as the tap would."""
    detector = Detector(config)
    readings, t, seq = [], 0.0, 0
    for duration, lit_at in phases:
        for _ in range(round(duration * hz)):
            seq += 1
            values = {name: LIT if lit else DARK for name, lit in lit_at(t).items()}
            readings.append(
                detector.update_values(
                    t=t,
                    wall=W0 + timedelta(seconds=t),
                    seq=seq,
                    values=values,
                    dark_value=DARK,
                )
            )
            t += 1.0 / hz
    return readings


def dark(_t):
    return {"red": False, "green": False}


def red_solid(_t):
    return {"red": True, "green": False}


def green_solid(_t):
    return {"red": False, "green": True}


def red_blinking(hz=1.5, duty=0.5):
    return lambda t: {"red": (t * hz) % 1.0 < duty, "green": False}


def green_blinking(hz=1.0, duty=0.5):
    """Completion. Measured on the machine at about 1Hz, 50% duty -- the controller
    pulsing the green leg, which is also the relay you can hear clicking."""
    return lambda t: {"red": False, "green": (t * hz) % 1.0 < duty}


@pytest.fixture
def machine1():
    return load_config(CONFIG_DIR / "machine1.yaml")


def test_a_latched_error_commits_once_and_holds(machine1):
    """The light stays lit for as long as it takes someone to deal with it, so the
    node must announce it once and then stay quiet."""
    readings = drive(machine1, [(5.0, dark), (60.0, red_solid), (10.0, dark)])
    assert [state for state, _ in commits(readings)] == ["clear", "error", "clear"]


def test_a_blinking_error_is_still_an_error(machine1):
    """The whole point of `on`: the notification must not depend on knowing whether
    this particular light blinks."""
    readings = drive(machine1, [(5.0, dark), (40.0, red_blinking()), (10.0, dark)])
    assert [state for state, _ in commits(readings)] == ["clear", "error", "clear"]
    lit = next(r for r in readings if 20.0 < r.t < 40.0)
    assert lit.segments["red"].mode is FLASHING  # measured and published...
    assert lit.state == "error"  # ...but not depended on


def test_a_run_commits_running_then_done(machine1):
    """The sequence the machine actually produces, and the one the original config got
    backwards: solid green while the cycle runs, flashing green once it finishes."""
    readings = drive(
        machine1, [(5.0, dark), (40.0, green_solid), (40.0, green_blinking()), (10.0, dark)]
    )
    assert [state for state, _ in commits(readings)] == ["clear", "running", "done", "clear"]


def test_steady_green_is_not_a_completed_run(machine1):
    """Guards the original bug directly: a cycle that starts and is still going must
    never resolve to `done`, because that fires a completion alert at the start."""
    readings = drive(machine1, [(5.0, dark), (40.0, green_solid)])
    assert [state for state, _ in commits(readings)] == ["clear", "running"]


def test_a_light_that_switches_from_blinking_to_solid_does_not_renotify(machine1):
    """If the light settles from a blink to a steady lamp, nothing has changed about
    what the operator needs to do, so it must not fire a second notification."""
    readings = drive(machine1, [(5.0, dark), (20.0, red_blinking()), (20.0, red_solid)])
    assert [state for state, _ in commits(readings)] == ["clear", "error"]


def test_error_then_done_is_two_notifications(machine1):
    readings = drive(
        machine1, [(5.0, dark), (30.0, red_solid), (10.0, dark), (30.0, green_blinking())]
    )
    assert [state for state, _ in commits(readings)] == ["clear", "error", "clear", "done"]


def test_dwell_measures_response_time_not_machine_time(machine1):
    """`since` on a latched state is how long the light sat waiting for the operator,
    which is response time rather than machine time. Run duration is a separate thing
    and is measurable: it is the interval from `running` to `done`."""
    lit_for = 45.0
    readings = drive(machine1, [(5.0, dark), (lit_for, red_solid)])
    last = readings[-1]
    assert last.state == "error"
    assert last.since_t is not None

    # `since` is pinned to when the pattern was recognised, which trails the lamp
    # coming on by however long the rolling window takes to clear the steady boundary.
    ramp = machine1.detection.flash_window_s * machine1.detection.flash_duty_range[1]
    assert last.t - last.since_t == pytest.approx(lit_for - ramp, abs=0.5)


# -- the same model over the camera ------------------------------------------------


def test_the_camera_reaches_the_same_verdicts(config_dict):
    """The annunciator model is not tap-specific. A camera watching the same latched
    light resolves identically."""
    config_dict["states"] = {
        "error": {"red": "on"},
        "done": {"red": "off", "green": "on"},
        "clear": {"red": "off", "green": "off"},
    }
    config = parse_config(config_dict)
    source = create_source(config, override="synthetic", clock=VirtualClock())
    source.set_script(
        [
            off_step(6.0, ["red", "green"]),
            ScriptStep(10.0, {"red": STEADY}),
            off_step(6.0, ["red", "green"]),
        ],
        loop=False,
    )
    detector = Detector(config)
    with source:
        readings = [
            detector.update(frame) for frame in source.frames(limit=round(22.0 * config.camera.fps))
        ]
    assert [state for state, _ in commits(readings)] == ["clear", "error", "clear"]
