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


# -- the real gpiozero-backed reader -----------------------------------------------
#
# These exist because the bug they pin down got all the way to real hardware: the
# fake reader bypassed GpiozeroPinReader entirely, so nothing tested the one piece
# where polarity is actually decided.


class FakeDigitalInputDevice:
    def __init__(self, pin, pull_up=None, active_state=None):
        self.pin = pin
        self.pull_up = pull_up
        self.active_state = active_state
        self.closed = False
        self.value = 0

    def close(self):
        self.closed = True


@pytest.fixture
def fake_gpiozero(monkeypatch):
    import sys
    import types

    created: list[FakeDigitalInputDevice] = []

    def factory(pin, **kwargs):
        device = FakeDigitalInputDevice(pin, **kwargs)
        created.append(device)
        return device

    module = types.ModuleType("gpiozero")
    module.DigitalInputDevice = factory
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    return created


def reader(fake_gpiozero, *, active_low=True, pull="up", pins=(17, 27)):
    from stacklight.sources.gpio import GpiozeroPinReader

    return GpiozeroPinReader(pins, active_low=active_low, pull=pull), fake_gpiozero


def test_a_sinking_opto_is_configured_as_pull_up(fake_gpiozero):
    _, devices = reader(fake_gpiozero, active_low=True, pull="up")
    assert [d.pull_up for d in devices] == [True, True]
    # active_state must be left alone: gpiozero derives it from the pull, and setting
    # both is an error.
    assert [d.active_state for d in devices] == [None, None]


def test_a_sourcing_opto_is_configured_as_pull_down(fake_gpiozero):
    _, devices = reader(fake_gpiozero, active_low=False, pull="down")
    assert [d.pull_up for d in devices] == [False, False]


def test_no_pull_requires_an_explicit_active_state(fake_gpiozero):
    _, devices = reader(fake_gpiozero, active_low=True, pull="none")
    assert devices[0].pull_up is None
    assert devices[0].active_state is False  # active when low


def test_an_unconnected_pull_up_input_reads_dark(fake_gpiozero):
    """The regression. gpiozero's value already means "is active" once a pull is set,
    so inverting it again reported every segment permanently lit with nothing even
    wired up."""
    pin_reader, devices = reader(fake_gpiozero, active_low=True, pull="up")
    devices[0].value = 0  # nothing dragging the pin down
    assert pin_reader.read(17) is False


def test_an_energised_opto_reads_lit(fake_gpiozero):
    pin_reader, devices = reader(fake_gpiozero, active_low=True, pull="up")
    devices[0].value = 1  # opto sinking the pin
    assert pin_reader.read(17) is True


def test_closing_releases_every_pin(fake_gpiozero):
    pin_reader, devices = reader(fake_gpiozero)
    pin_reader.close()
    assert all(d.closed for d in devices)


def test_a_pin_that_cannot_be_claimed_releases_the_others(fake_gpiozero):
    """Otherwise a restart loop leaves half the pins held by a dead process."""
    import sys

    claimed: list[FakeDigitalInputDevice] = []

    def factory(pin, **kwargs):
        if pin == 27:
            raise RuntimeError("GPIO busy")
        device = FakeDigitalInputDevice(pin, **kwargs)
        claimed.append(device)
        return device

    sys.modules["gpiozero"].DigitalInputDevice = factory
    from stacklight.sources.gpio import GpiozeroPinReader

    with pytest.raises(SourceError, match="could not claim GPIO pins"):
        GpiozeroPinReader((17, 27), active_low=True, pull="up")
    assert claimed and all(d.closed for d in claimed)


# -- config guards against the contradiction that caused it ------------------------


def test_pull_up_with_active_high_is_rejected():
    data = gpio_config_dict()
    data["gpio"] = {"active_low": False, "pull": "up"}
    with pytest.raises(ConfigError, match="contradicts active_low"):
        parse_config(data)


def test_pull_down_with_active_low_is_rejected():
    """The pull would fight the optocoupler and the input would never change."""
    data = gpio_config_dict()
    data["gpio"] = {"active_low": True, "pull": "down"}
    with pytest.raises(ConfigError, match="contradicts active_low"):
        parse_config(data)


# -- the tap node publishes to MQTT ------------------------------------------------


def run_node(phases, *, hz=100.0):
    """Drive a full Node from fake pins and record what it publishes."""
    import json

    from stacklight.node import Node
    from stacklight.publisher import RecordingPublisher

    reader = FakePinReader()
    clock = VirtualClock()
    config = parse_config(gpio_config_dict())
    sampler = GpioSampler.from_config(config, reader=reader, clock=clock)
    publisher = RecordingPublisher()
    node = Node(config, sampler, publisher)

    with node:
        for duration, active in phases:
            for pin in (17, 27, 22):
                reader.set(pin, pin in active)
            node.run(max_frames=round(duration * hz))

    def payloads(topic):
        return [json.loads(m.payload) for m in publisher.on(topic)]

    return config, publisher, payloads


def test_the_tap_node_publishes_committed_states():
    """Pins to MQTT with no camera anywhere in the chain."""
    config, _, payloads = run_node([(4.0, ()), (20.0, (17,)), (6.0, ()), (20.0, (27,))])
    assert [p["state"] for p in payloads(config.state_topic)] == [
        "clear",
        "error",
        "clear",
        "done",
    ]


def test_state_payloads_carry_the_raw_pin_values():
    config, _, payloads = run_node([(4.0, ()), (20.0, (17,))])
    lit = payloads(config.state_topic)[-1]
    assert lit["state"] == "error"
    assert lit["segments"]["red"]["value"] == 255.0
    assert lit["segments"]["green"]["value"] == 0.0
    assert lit["segments"]["buzzer"]["value"] == 0.0


def test_a_tap_node_publishes_no_snapshots():
    """There are no pixels to encode. It skips rather than erroring."""
    config, publisher, _ = run_node([(4.0, ()), (20.0, (17,))])
    assert publisher.on(config.snapshot_topic) == []


def test_heartbeats_report_the_tap_as_the_source():
    config, _, payloads = run_node([(20.0, ())])
    beat = payloads(config.health_topic)[-1]
    assert beat["source"] == "gpio"
    assert beat["dark_reference"] is None
    assert "buzzer" in beat["segments"]


def test_availability_still_brackets_the_run():
    config, _, payloads = run_node([(6.0, ())])
    assert [p["status"] for p in payloads(config.availability_topic)] == [
        "online",
        "offline",
    ]


def test_the_buzzer_is_published_without_changing_the_state():
    config, _, payloads = run_node([(4.0, ()), (20.0, (17, 22))])
    lit = payloads(config.state_topic)[-1]
    assert lit["state"] == "error"
    assert lit["segments"]["buzzer"]["value"] == 255.0
    assert lit["segments"]["buzzer"]["lit"] is True
