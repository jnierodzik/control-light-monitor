"""The GPIO tap: config without a camera, and the sampler that feeds the detector.

Everything here runs on a laptop. The pin-reading backend is injected, so no Pi, no
gpiozero and no hardware is involved.
"""

from __future__ import annotations

import pytest

from stacklight.clock import VirtualClock
from stacklight.config import load_config, parse_config
from stacklight.detector import Detector
from stacklight.errors import ConfigError, SourceError
from stacklight.sources.gpio import DARK, LIT, FakePinReader, GpioSampler

from .conftest import CONFIG_DIR, commits


def gpio_config_dict() -> dict:
    return {
        "node_id": "tap",
        "sensor": "gpio",
        "segments": [
            {"name": "red", "pin": 17},
            {"name": "green", "pin": 27},
            {"name": "buzzer", "pin": 22},
        ],
        "gpio": {"sample_hz": 100},
        "detection": {"debounce_s": 1.0, "flash_window_s": 2.0},
        "states": {
            "error": {"red": "on"},
            "done": {"red": "off", "green": "on"},
            "clear": {"red": "off", "green": "off"},
        },
    }


# -- config without a camera -------------------------------------------------------


def test_a_gpio_node_needs_no_camera_section():
    """The whole point of standalone: no camera hardware, no camera config."""
    config = parse_config(gpio_config_dict())
    assert config.sensor == "gpio"
    assert config.camera is None
    assert config.dark_reference is None
    assert [seg.pin for seg in config.segments] == [17, 27, 22]


def test_the_shipped_tap_config_is_valid():
    config = load_config(CONFIG_DIR / "machine1.yaml")
    assert config.sensor == "gpio"
    assert config.camera is None
    assert config.segment_names == ("red", "green", "buzzer")


def test_the_buzzer_is_tapped_but_inert_in_state_resolution():
    """Published for observation, but no state names it, so it cannot change a verdict
    until its meaning is known."""
    from stacklight.types import SegmentMode as M

    config = load_config(CONFIG_DIR / "machine1.yaml")
    for buzzer in M:
        assert config.resolve_state({"red": M.OFF, "green": M.OFF, "buzzer": buzzer}) == "clear"
        assert config.resolve_state({"red": M.STEADY, "green": M.OFF, "buzzer": buzzer}) == "error"


def test_a_gpio_segment_without_a_pin_is_rejected():
    data = gpio_config_dict()
    del data["segments"][0]["pin"]
    with pytest.raises(ConfigError, match="missing required key 'pin'"):
        parse_config(data)


def test_duplicate_pins_are_rejected():
    """Two segments on one pin would read identically for ever, silently."""
    data = gpio_config_dict()
    data["segments"][1]["pin"] = 17
    with pytest.raises(ConfigError, match="pin 17 is already used by 'red'"):
        parse_config(data)


def test_camera_only_keys_are_rejected_on_a_gpio_node():
    data = gpio_config_dict()
    data["segments"][0]["roi"] = [0, 0, 8, 8]
    with pytest.raises(ConfigError, match="only applies when sensor is 'camera'"):
        parse_config(data)


def test_a_camera_section_on_a_gpio_node_is_rejected():
    """Rejected rather than ignored: a camera block here means someone misunderstood
    which sensor they are configuring."""
    data = gpio_config_dict()
    data["camera"] = {
        "source": "synthetic",
        "resolution": [64, 48],
        "fps": 10,
        "exposure_us": 800,
        "analogue_gain": 1.0,
    }
    with pytest.raises(ConfigError, match="camera: only applies when sensor is 'camera'"):
        parse_config(data)


def test_a_pin_on_a_camera_node_is_rejected(config_dict):
    config_dict["segments"][0]["pin"] = 17
    with pytest.raises(ConfigError, match="only applies when sensor is 'gpio'"):
        parse_config(config_dict)


def test_camera_remains_required_for_a_camera_node(config_dict):
    del config_dict["camera"]
    with pytest.raises(ConfigError, match="missing required key 'camera'"):
        parse_config(config_dict)


# -- the sampler -------------------------------------------------------------------


def make_sampler(reader=None, clock=None):
    config = parse_config(gpio_config_dict())
    return GpioSampler.from_config(
        config, reader=reader or FakePinReader(), clock=clock or VirtualClock()
    ), config


def test_an_energised_lead_reads_lit_and_a_dark_one_reads_dark():
    reader = FakePinReader({17: True})
    sampler, _ = make_sampler(reader)
    with sampler:
        sample = sampler.read()
    assert sample.values == {"red": LIT, "green": DARK, "buzzer": DARK}
    assert sample.lit() == {"red": True, "green": False, "buzzer": False}


def test_sampling_is_paced_to_the_configured_rate():
    clock = VirtualClock()
    sampler, _ = make_sampler(clock=clock)
    with sampler:
        samples = list(sampler.samples(limit=101))
    assert samples[-1].t - samples[0].t == pytest.approx(1.0, abs=0.02)  # 100Hz
    assert [s.seq for s in samples[:3]] == [1, 2, 3]


def test_reading_before_start_is_an_error():
    sampler, _ = make_sampler()
    with pytest.raises(SourceError, match="before start"):
        sampler.read()


def test_an_injected_reader_is_not_closed_by_the_sampler():
    """The caller owns what it injected. Only a reader the sampler created is freed."""
    reader = FakePinReader()
    sampler, _ = make_sampler(reader)
    with sampler:
        sampler.read()
    assert reader.closed is False
    assert sampler.started is False


def test_from_config_refuses_a_camera_config(config):
    with pytest.raises(SourceError, match="is sensor set to 'gpio'"):
        GpioSampler.from_config(config)


def test_pins_come_from_the_config():
    sampler, _ = make_sampler()
    assert sampler.pins == {"red": 17, "green": 27, "buzzer": 22}
    assert sampler.sample_interval == pytest.approx(0.01)


# -- sampler straight into the detector --------------------------------------------


def run_tap(phases, *, hz=100.0):
    """Drive pins through the real sampler and the real detector."""
    reader = FakePinReader()
    clock = VirtualClock()
    config = parse_config(gpio_config_dict())
    sampler = GpioSampler.from_config(config, reader=reader, clock=clock)
    detector = Detector(config)
    readings = []
    with sampler:
        for duration, active in phases:
            for pin in (17, 27, 22):
                reader.set(pin, pin in active)
            for sample in sampler.samples(limit=round(duration * hz)):
                readings.append(
                    detector.update_values(
                        t=sample.t, wall=sample.wall, seq=sample.seq, values=sample.values
                    )
                )
    return readings


def test_the_tap_drives_the_annunciator_end_to_end():
    """Pins -> sampler -> detector -> committed state, with no camera anywhere."""
    readings = run_tap([(4.0, ()), (20.0, (17,)), (6.0, ()), (20.0, (27,)), (6.0, ())])
    assert [state for state, _ in commits(readings)] == [
        "clear",
        "error",
        "clear",
        "done",
        "clear",
    ]


def test_the_buzzer_is_measured_and_published_without_changing_the_verdict():
    readings = run_tap([(4.0, ()), (20.0, (17, 22)), (6.0, ())])
    lit = next(r for r in readings if 15.0 < r.t < 20.0)
    assert lit.state == "error"
    assert lit.values["buzzer"] == LIT
    assert lit.segments["buzzer"].lit is True


def test_an_error_that_outlives_the_buzzer_does_not_renotify():
    """The buzzer going quiet while red stays lit is not a new event."""
    readings = run_tap([(4.0, ()), (15.0, (17, 22)), (15.0, (17,)), (6.0, ())])
    assert [state for state, _ in commits(readings)] == ["clear", "error", "clear"]
