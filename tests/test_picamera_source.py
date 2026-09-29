"""PiCameraSource tests against a fake picamera2.

There is no camera on a dev machine, but the part that matters most here is not pixel
handling -- it is whether every automatic behaviour actually gets switched off. That is
pure control-dictionary construction, and it can be asserted exactly.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import numpy as np
import pytest

from stacklight.config import parse_config
from stacklight.errors import SourceError
from stacklight.sources import create_source


@dataclass
class FakePicamera2:
    configured: dict | None = None
    created_with: dict = field(default_factory=dict)
    started: bool = False
    stopped: bool = False
    closed: bool = False
    control_calls: list[dict] = field(default_factory=list)
    frame: np.ndarray | None = None
    capture_error: Exception | None = None
    start_error: Exception | None = None
    metadata: dict = field(default_factory=lambda: {"ExposureTime": 800, "AnalogueGain": 1.0})

    def create_video_configuration(self, **kwargs):
        self.created_with = kwargs
        return {"main": kwargs.get("main", {}), "sensor": {"output_size": (1536, 864)}}

    def configure(self, camera_config):
        self.configured = camera_config

    def camera_configuration(self):
        return self.configured or {}

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def set_controls(self, controls):
        self.control_calls.append(dict(controls))

    def capture_metadata(self):
        return dict(self.metadata)

    def capture_array(self, _name="main"):
        if self.capture_error is not None:
            raise self.capture_error
        if self.frame is None:
            raise AssertionError("test did not set a frame")
        return self.frame


@pytest.fixture
def fake_picamera2(monkeypatch):
    """Install fake picamera2 and libcamera modules for the duration of a test."""
    instances: list[FakePicamera2] = []

    def factory():
        instance = FakePicamera2()
        instances.append(instance)
        return instance

    picamera2_module = types.ModuleType("picamera2")
    picamera2_module.Picamera2 = factory
    monkeypatch.setitem(sys.modules, "picamera2", picamera2_module)

    libcamera = types.ModuleType("libcamera")
    controls_module = types.SimpleNamespace(AfModeEnum=types.SimpleNamespace(Manual="MANUAL"))
    libcamera.controls = controls_module
    monkeypatch.setitem(sys.modules, "libcamera", libcamera)
    monkeypatch.setitem(sys.modules, "libcamera.controls", controls_module)

    return instances


def pi_config(config_dict, **camera):
    config_dict["camera"].update({"source": "picamera", **camera})
    config_dict["camera"].pop("synthetic", None)
    return parse_config(config_dict)


def bgr_frame(width=160, height=120, colour=(255, 0, 0)):
    """A frame as picamera2 hands it over: an "RGB888" stream, B-G-R in memory."""
    red, green, blue = colour
    data = np.zeros((height, width, 3), dtype=np.uint8)
    data[:, :] = (blue, green, red)
    return data


# -- everything automatic is switched off ------------------------------------------


def test_every_automatic_behaviour_is_disabled(config_dict, fake_picamera2):
    """The whole detection approach depends on the camera not adapting to the scene."""
    config = pi_config(config_dict, exposure_us=800, analogue_gain=1.0, lens_position=3.5)
    source = create_source(config)
    source._picam = None
    with source:
        controls = fake_picamera2[0].created_with["controls"]

    assert controls["AeEnable"] is False
    assert controls["AwbEnable"] is False
    assert controls["AfMode"] == "MANUAL"
    assert controls["ExposureTime"] == 800
    assert controls["AnalogueGain"] == 1.0
    assert controls["LensPosition"] == 3.5


def test_frame_duration_is_pinned_at_both_ends(config_dict, fake_picamera2):
    """A single limit would let the camera lengthen exposure to chase brightness."""
    config = pi_config(config_dict, lens_position=3.5)
    config_dict["camera"]["fps"] = 20
    with create_source(config):
        controls = fake_picamera2[0].created_with["controls"]
    assert controls["FrameDurationLimits"] == (50_000, 50_000)


def test_colour_gains_are_stated_when_awb_is_off(config_dict, fake_picamera2):
    """With AWB disabled the ISP keeps whatever gains it last had, so leaving them
    unset makes colour balance undefined between runs."""
    config = pi_config(config_dict, awb_enable=False, colour_gains=[1.4, 2.1])
    with create_source(config):
        controls = fake_picamera2[0].created_with["controls"]
    assert controls["ColourGains"] == (1.4, 2.1)


def test_colour_gains_are_left_alone_when_awb_is_on(config_dict, fake_picamera2):
    config = pi_config(config_dict, awb_enable=True)
    with create_source(config):
        controls = fake_picamera2[0].created_with["controls"]
    assert "ColourGains" not in controls


def test_no_lens_position_leaves_focus_automatic_and_warns(config_dict, fake_picamera2, caplog):
    config = pi_config(config_dict)
    assert config.camera.lens_position is None
    with caplog.at_level("WARNING"), create_source(config):
        controls = fake_picamera2[0].created_with["controls"]
    assert "AfMode" not in controls
    assert "LensPosition" not in controls
    assert "focus is left on automatic" in caplog.text


def test_an_exposure_longer_than_the_frame_interval_warns(config_dict, fake_picamera2, caplog):
    """The camera silently runs slower, which quietly breaks flash detection."""
    config_dict["camera"]["fps"] = 15  # 66666us per frame
    config = pi_config(config_dict, exposure_us=100_000, lens_position=3.5)
    with caplog.at_level("WARNING"), create_source(config):
        pass
    assert "longer than the frame interval" in caplog.text


# -- channel order -----------------------------------------------------------------


def test_picamera_byte_order_is_corrected_to_rgb(config_dict, fake_picamera2):
    """picamera2's "RGB888" arrives B-G-R in memory. A red LED must land in channel 0,
    or snapshots come out with red and blue swapped."""
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)
    with source:
        fake_picamera2[0].frame = bgr_frame(colour=(255, 0, 0))
        frame = source.read()
    assert tuple(frame.data[0, 0]) == (255, 0, 0)


def test_swap_rb_false_passes_the_array_through_untouched(config_dict, fake_picamera2):
    config = pi_config(config_dict, lens_position=3.5, swap_rb=False)
    source = create_source(config)
    with source:
        fake_picamera2[0].frame = bgr_frame(colour=(255, 0, 0))
        frame = source.read()
    assert tuple(frame.data[0, 0]) == (0, 0, 255)


def test_detection_is_indifferent_to_channel_order(config_dict, fake_picamera2):
    """Why getting swap_rb wrong is survivable: the default metric takes the max
    channel, so a lit segment reads the same either way."""
    from stacklight.metrics import measure

    config = pi_config(config_dict, lens_position=3.5)
    swapped = pi_config(dict(config_dict), lens_position=3.5, swap_rb=False)
    roi = config.segment("red").roi

    values = []
    for cfg in (config, swapped):
        source = create_source(cfg)
        with source:
            fake_picamera2[-1].frame = bgr_frame(colour=(255, 0, 0))
            values.append(measure(source.read().data, roi, "value"))
    assert values[0] == values[1] == 255.0


def test_a_four_channel_frame_is_trimmed_to_three(config_dict, fake_picamera2):
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)
    with source:
        fake_picamera2[0].frame = np.zeros((120, 160, 4), dtype=np.uint8)
        frame = source.read()
    assert frame.data.shape == (120, 160, 3)


def test_an_unexpected_frame_shape_is_an_error(config_dict, fake_picamera2):
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)
    with source:
        fake_picamera2[0].frame = np.zeros((120, 160), dtype=np.uint8)
        with pytest.raises(SourceError, match="unexpected frame shape"):
            source.read()


# -- live control changes ----------------------------------------------------------


def test_controls_can_be_changed_on_a_running_camera(config_dict, fake_picamera2):
    """This is what makes the calibration UI's exposure slider work on real hardware."""
    config = pi_config(config_dict, exposure_us=800, lens_position=3.5)
    source = create_source(config)
    with source:
        source.set_controls(exposure_us=2400, analogue_gain=2.0)
    applied = fake_picamera2[0].control_calls[-1]
    assert applied["ExposureTime"] == 2400
    assert applied["AnalogueGain"] == 2.0
    assert applied["AeEnable"] is False  # still locked


def test_a_control_change_before_start_is_not_pushed(config_dict, fake_picamera2):
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)
    source.set_controls(exposure_us=1200)
    assert source.controls.exposure_us == 1200  # recorded, for start() to apply


def test_applied_controls_are_read_back_from_the_camera(config_dict, fake_picamera2):
    """A sensor silently clamps an out-of-range exposure, so what it accepted matters
    more than what was asked for."""
    config = pi_config(config_dict, exposure_us=10, lens_position=3.5)
    source = create_source(config)
    with source:
        pass
    assert source.applied_controls["ExposureTime"] == 800  # what the fake reported back


# -- lifecycle and failures --------------------------------------------------------


def test_the_camera_is_released_on_stop(config_dict, fake_picamera2):
    config = pi_config(config_dict, lens_position=3.5)
    with create_source(config):
        pass
    assert fake_picamera2[0].stopped is True
    assert fake_picamera2[0].closed is True


def test_a_failure_to_start_releases_the_camera(config_dict, fake_picamera2):
    """Otherwise a restart loop leaves the camera held by a dead process."""
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)
    instance: list = []

    original = source._import_picamera2

    def instrumented():
        module = original()
        real_factory = module.Picamera2

        def factory():
            cam = real_factory()
            cam.start_error = RuntimeError("camera busy")
            instance.append(cam)
            return cam

        module.Picamera2 = factory
        return module

    source._import_picamera2 = instrumented
    with pytest.raises(SourceError, match="could not start the Pi camera"):
        source.start()
    assert instance[0].closed is True
    assert source.started is False


def test_a_capture_failure_surfaces_as_a_source_error(config_dict, fake_picamera2):
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)
    with source:
        fake_picamera2[0].capture_error = RuntimeError("timed out")
        with pytest.raises(SourceError, match="pi camera capture failed"):
            source.read()


def test_the_sensor_sets_the_pace_rather_than_a_sleep(config_dict, fake_picamera2):
    """FrameDurationLimits already pins the rate and capture_array blocks for the next
    frame. Sleeping to a slot on top of that would halve the effective frame rate."""
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)
    with source:
        fake_picamera2[0].frame = bgr_frame()
        stamps = [source.read().t for _ in range(5)]
    # No pacing delay was imposed: five frames came back with no wall time in between.
    assert max(stamps) - min(stamps) < 0.1


def test_the_module_is_not_imported_until_the_camera_starts(config_dict, monkeypatch):
    """`import stacklight` must work on a machine with no picamera2 at all, which is
    every machine except the Pi."""
    monkeypatch.setitem(sys.modules, "picamera2", None)
    config = pi_config(config_dict, lens_position=3.5)
    source = create_source(config)  # constructing is fine
    assert source.kind == "picamera"
    with pytest.raises(SourceError, match="apt install python3-picamera2"):
        source.start()
