"""Per-node YAML configuration: parsing, validation, and state resolution.

Everything the node does is driven from here. Nothing about the machine in particular
is baked into the code: segment names, ROIs, thresholds and the meaning of each
segment pattern all come from the config file.

Validation is strict and total. Unknown keys are rejected rather than ignored, because
a silently-ignored typo in an ROI is the kind of bug that costs an afternoon on a
ladder with a laptop.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .types import BRIGHTNESS_METRICS, UNKNOWN_STATE, SegmentMode
from .validation import (
    _as_mapping,
    _boolean,
    _fail,
    _number,
    _pair,
    _reject_unknown,
    _require,
    _string,
)

NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CAMERA_SOURCES = ("picamera", "file", "synthetic")

#: How a node senses the light. "gpio" needs no camera hardware or config at all.
SENSORS = ("camera", "gpio")

#: Fallback render colours for SyntheticSource when a segment declares none.
_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 40, 30),
    "green": (40, 255, 70),
    "amber": (255, 170, 20),
    "yellow": (255, 230, 40),
    "orange": (255, 130, 20),
    "blue": (50, 90, 255),
    "white": (255, 255, 255),
    "clear": (255, 255, 255),
}


# --------------------------------------------------------------------------------------
# Small validation helpers. Each carries a dotted path so errors point at the exact key.
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Config dataclasses
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Roi:
    """An axis-aligned region of interest in frame pixel coordinates."""

    x: int
    y: int
    w: int
    h: int

    @classmethod
    def parse(cls, value: Any, path: str) -> Roi:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
            _fail(path, f"expected [x, y, w, h], got {value!r}")
        x, y, w, h = (
            _number(v, f"{path}[{i}]", integer=True, minimum=0) for i, v in enumerate(value)
        )
        if w <= 0 or h <= 0:
            _fail(path, f"width and height must be positive, got w={w} h={h}")
        return cls(x, y, w, h)

    def check_within(self, width: int, height: int, path: str) -> None:
        if self.x + self.w > width or self.y + self.h > height:
            _fail(
                path,
                f"roi {self.as_list()} extends past the {width}x{height} frame "
                f"(right edge {self.x + self.w}, bottom edge {self.y + self.h})",
            )

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]

    @property
    def slices(self) -> tuple[slice, slice]:
        """``(rows, cols)`` slices for indexing a ``(H, W, ...)`` array."""
        return slice(self.y, self.y + self.h), slice(self.x, self.x + self.w)

    @property
    def area(self) -> int:
        return self.w * self.h


@dataclass(frozen=True)
class SegmentConfig:
    """One light segment on the pole."""

    name: str
    #: Where the segment is in frame. Camera nodes only.
    roi: Roi | None
    threshold: float
    #: BCM pin the segment's optocoupler output lands on. GPIO nodes only.
    pin: int | None = None
    #: Nominal lit colour. Used for the optional colour sanity check and by
    #: SyntheticSource when rendering this segment. Inferred from the name when omitted.
    color: tuple[int, int, int] | None = None

    @property
    def render_color(self) -> tuple[int, int, int]:
        if self.color is not None:
            return self.color
        return _NAMED_COLORS.get(self.name.lower(), (255, 255, 255))


@dataclass(frozen=True)
class DarkReferenceConfig:
    """A region that must always read dark.

    If it starts reading bright, the camera has been bumped or the lighting changed,
    and every other reading in the frame is suspect.
    """

    roi: Roi
    threshold: float


@dataclass(frozen=True)
class SyntheticSegmentSpec:
    name: str
    roi: Roi
    color: tuple[int, int, int]


@dataclass(frozen=True)
class SyntheticConfig:
    """Knobs for the fully synthetic frame source."""

    lit_value: float = 210.0
    noise_sigma: float = 1.5
    ambient: float = 4.0
    glow_px: int = 0
    flash_hz: float = 1.5
    flash_duty: float = 0.5
    realtime: bool = True
    seed: int = 0
    #: When empty, SyntheticSource mirrors the detection segments, so any real config
    #: can be replayed without hardware just by switching ``camera.source``.
    segments: tuple[SyntheticSegmentSpec, ...] = ()


@dataclass(frozen=True)
class FileSourceConfig:
    path: Path
    loop: bool = True
    pattern: str = "*"
    realtime: bool = True


@dataclass(frozen=True)
class CameraConfig:
    source: str
    resolution: tuple[int, int]
    fps: float
    exposure_us: int
    analogue_gain: float
    awb_enable: bool
    lens_position: float | None
    file: FileSourceConfig | None
    synthetic: SyntheticConfig
    #: Red and blue ISP gains, applied only when AWB is off. With auto-white-balance
    #: disabled the ISP keeps whatever gains it last had, so they have to be stated or
    #: the colour balance is undefined.
    colour_gains: tuple[float, float] = (1.0, 1.0)
    #: picamera2 delivers an "RGB888" stream as B, G, R in memory, so the channels are
    #: swapped back by default. Set false if snapshot colours come out inverted.
    swap_rb: bool = True
    buffer_count: int = 4

    @property
    def width(self) -> int:
        return self.resolution[0]

    @property
    def height(self) -> int:
        return self.resolution[1]


@dataclass(frozen=True)
class GpioConfig:
    """Settings for a node that reads the lamp wiring rather than watching it."""

    #: Poll rate. Far above the camera's 15fps, which is the point: a blink is resolved
    #: by tens of samples per cycle rather than one or two frames.
    sample_hz: float = 100.0
    #: Optocoupler outputs usually pull the pin DOWN when the field input is energised,
    #: so a low pin means a lit lamp. Flip if your board sources instead of sinks.
    active_low: bool = True
    #: Internal pull on the Pi's input. Must oppose the opto's active level, or a
    #: floating pin reads as whatever it last saw.
    pull: str = "up"


@dataclass(frozen=True)
class DetectionConfig:
    debounce_s: float
    flash_window_s: float
    flash_duty_range: tuple[float, float]
    flash_min_transitions: int
    #: How an ROI's pixels collapse to one number. "value" (max channel) is the default
    #: because it is hue-independent: a saturated red LED and a saturated green LED both
    #: read ~255, whereas a plain RGB average puts a full-brightness red at only ~85.
    metric: str = "value"


@dataclass(frozen=True)
class PublishConfig:
    heartbeat_s: float = 15.0
    state_refresh_s: float = 60.0
    snapshot_min_interval_s: float = 300.0
    snapshot_quality: int = 70
    snapshot_max_width: int = 640
    #: Publish a snapshot on every committed transition, bypassing the rate limit, so a
    #: notification carries an image of the state that actually triggered it rather than
    #: whatever happened to be captured minutes earlier.
    snapshot_on_transition: bool = True
    #: Outline each ROI and print its measured value on the snapshot.
    snapshot_annotate: bool = True
    #: Brightness multiplier applied to snapshots only. The detector never sees this.
    #: Raise it to make the scene around the LEDs visible when checking camera aim.
    snapshot_gain: float = 1.0


@dataclass(frozen=True)
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    base_topic: str = "shop/lights"
    username: str | None = None
    password: str | None = None
    keepalive: int = 60
    client_id: str | None = None
    tls: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"


@dataclass(frozen=True)
class CalibrationConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    sample_dir: Path = Path("samples")


@dataclass(frozen=True)
class ModeSet:
    """The set of segment modes that satisfy one state constraint.

    Usually a single mode. The exception is ``on``, which matches ``steady`` or
    ``flashing`` -- for an annunciator where red means "error" whether it is solid or
    blinking, depending on which would be depending on a distinction that may not exist.
    """

    modes: frozenset[SegmentMode]
    #: The token written in the config, kept for error messages and display.
    label: str

    def matches(self, mode: SegmentMode | None) -> bool:
        return mode in self.modes

    def __str__(self) -> str:
        return self.label


#: Tokens accepted on the right-hand side of a state constraint.
MODE_TOKENS: dict[str, ModeSet] = {
    SegmentMode.OFF.value: ModeSet(frozenset({SegmentMode.OFF}), SegmentMode.OFF.value),
    SegmentMode.STEADY.value: ModeSet(frozenset({SegmentMode.STEADY}), SegmentMode.STEADY.value),
    SegmentMode.FLASHING.value: ModeSet(
        frozenset({SegmentMode.FLASHING}), SegmentMode.FLASHING.value
    ),
    "on": ModeSet(frozenset({SegmentMode.STEADY, SegmentMode.FLASHING}), "on"),
}


@dataclass(frozen=True)
class StateSpec:
    """A named state, defined as constraints on some or all segments.

    Segments the spec does not mention are wildcards. ``{"red": "flashing"}`` matches
    whenever red is flashing, whatever green is doing.
    """

    name: str
    constraints: Mapping[str, ModeSet]

    @property
    def specificity(self) -> int:
        return len(self.constraints)

    def matches(self, observed: Mapping[str, SegmentMode]) -> bool:
        return all(allowed.matches(observed.get(seg)) for seg, allowed in self.constraints.items())


@dataclass(frozen=True)
class NodeConfig:
    """A complete, validated node configuration."""

    node_id: str
    sensor: str
    mqtt: MqttConfig
    #: None on a GPIO node. Nothing camera-shaped is required to run one.
    camera: CameraConfig | None
    gpio: GpioConfig
    segments: tuple[SegmentConfig, ...]
    dark_reference: DarkReferenceConfig | None
    detection: DetectionConfig
    publish: PublishConfig
    states: tuple[StateSpec, ...]
    logging: LoggingConfig
    calibration: CalibrationConfig
    path: Path | None = field(default=None, compare=False)

    @property
    def sample_rate_hz(self) -> float:
        """How often this node samples, whichever sensor it uses.

        A camera's frame rate and a tap's poll rate are the same quantity as far as
        anything downstream is concerned, and the health payload should not have to
        know which kind of node it is describing.
        """
        return self.gpio.sample_hz if self.camera is None else self.camera.fps

    @property
    def segment_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.segments)

    def segment(self, name: str) -> SegmentConfig:
        for seg in self.segments:
            if seg.name == name:
                return seg
        raise KeyError(name)

    @property
    def state_topic(self) -> str:
        return f"{self.mqtt.base_topic}/{self.node_id}/state"

    @property
    def health_topic(self) -> str:
        return f"{self.mqtt.base_topic}/{self.node_id}/health"

    @property
    def snapshot_topic(self) -> str:
        return f"{self.mqtt.base_topic}/{self.node_id}/snapshot"

    @property
    def availability_topic(self) -> str:
        """Retained online/offline flag, also registered as the MQTT last will.

        The heartbeat timeout in the rules engine is the backstop; this is the fast
        path, so a node that dies is noticed in seconds rather than in a minute.
        """
        return f"{self.mqtt.base_topic}/{self.node_id}/availability"

    def resolve_state(self, observed: Mapping[str, SegmentMode]) -> str:
        """Name the state for an observed segment pattern, or ``"unknown"``."""
        return resolve_state(self.states, observed)


# --------------------------------------------------------------------------------------
# State resolution
# --------------------------------------------------------------------------------------


def matching_states(
    states: Sequence[StateSpec], observed: Mapping[str, SegmentMode]
) -> list[StateSpec]:
    """Every spec that matches, most specific first, config order breaking ties."""
    matched = [
        (-spec.specificity, i, spec) for i, spec in enumerate(states) if spec.matches(observed)
    ]
    matched.sort(key=lambda item: (item[0], item[1]))
    return [spec for _, _, spec in matched]


def resolve_state(states: Sequence[StateSpec], observed: Mapping[str, SegmentMode]) -> str:
    """Resolve an observed pattern to a state name.

    The most specific matching state wins; an unmatched pattern resolves to
    ``"unknown"`` rather than to a guess.
    """
    matched = matching_states(states, observed)
    return matched[0].name if matched else UNKNOWN_STATE


def find_ambiguous_patterns(
    states: Sequence[StateSpec], segment_names: Sequence[str]
) -> list[tuple[dict[str, SegmentMode], list[str]]]:
    """Enumerate segment patterns that two states claim with equal specificity.

    Exhaustive over ``3 ** len(segment_names)`` patterns, so it is skipped for
    implausibly wide poles. Ties would otherwise be silently broken by config order.
    """
    modes = list(SegmentMode)
    if not states or len(modes) ** len(segment_names) > 4096:
        return []

    conflicts: list[tuple[dict[str, SegmentMode], list[str]]] = []
    for combo in itertools.product(modes, repeat=len(segment_names)):
        observed = dict(zip(segment_names, combo, strict=True))
        matched = matching_states(states, observed)
        if len(matched) > 1 and matched[0].specificity == matched[1].specificity:
            tied = [s.name for s in matched if s.specificity == matched[0].specificity]
            conflicts.append((observed, tied))
    return conflicts


# --------------------------------------------------------------------------------------
# Section parsers
# --------------------------------------------------------------------------------------


def _parse_mqtt(data: Any, path: str) -> MqttConfig:
    data = _as_mapping(data, path)
    allowed = (
        "host",
        "port",
        "base_topic",
        "username",
        "password",
        "keepalive",
        "client_id",
        "tls",
    )
    _reject_unknown(data, allowed, path)
    base_topic = _string(data.get("base_topic", "shop/lights"), f"{path}.base_topic")
    if base_topic.endswith("/") or base_topic.startswith("/"):
        _fail(f"{path}.base_topic", f"must not start or end with '/', got {base_topic!r}")
    if any(ch in base_topic for ch in "+#"):
        _fail(f"{path}.base_topic", "must not contain MQTT wildcards '+' or '#'")
    return MqttConfig(
        host=_string(data.get("host", "localhost"), f"{path}.host"),
        port=_number(
            data.get("port", 1883), f"{path}.port", integer=True, minimum=1, maximum=65535
        ),
        base_topic=base_topic,
        username=(
            None if data.get("username") is None else _string(data["username"], f"{path}.username")
        ),
        password=(
            None if data.get("password") is None else _string(data["password"], f"{path}.password")
        ),
        keepalive=_number(data.get("keepalive", 60), f"{path}.keepalive", integer=True, minimum=1),
        client_id=(
            None
            if data.get("client_id") is None
            else _string(data["client_id"], f"{path}.client_id")
        ),
        tls=_boolean(data.get("tls", False), f"{path}.tls"),
    )


def _parse_color(value: Any, path: str) -> tuple[int, int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        _fail(path, f"expected [r, g, b], got {value!r}")
    return tuple(
        _number(v, f"{path}[{i}]", integer=True, minimum=0, maximum=255)
        for i, v in enumerate(value)
    )


def _parse_segments(data: Any, path: str, sensor: str) -> tuple[SegmentConfig, ...]:
    """Parse the segment list.

    What a segment needs depends on how the node senses it: a camera segment is an ROI
    and a brightness threshold, a GPIO segment is a pin. Requiring both would force a
    GPIO node to invent ROIs it will never use.
    """
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        _fail(path, f"expected a list of segments, got {type(data).__name__}")
    if not data:
        _fail(path, "at least one segment is required")

    is_camera = sensor == "camera"
    segments: list[SegmentConfig] = []
    seen: set[str] = set()
    pins: dict[int, str] = {}
    for i, raw in enumerate(data):
        item_path = f"{path}[{i}]"
        item = _as_mapping(raw, item_path)
        _reject_unknown(item, ("name", "roi", "threshold", "color", "pin"), item_path)
        name = _string(_require(item, "name", item_path), f"{item_path}.name")
        if not name.strip():
            _fail(f"{item_path}.name", "must not be empty")
        if name == UNKNOWN_STATE:
            _fail(f"{item_path}.name", f"{UNKNOWN_STATE!r} is reserved")
        if name in seen:
            _fail(f"{item_path}.name", f"duplicate segment name {name!r}")
        seen.add(name)
        if is_camera:
            if item.get("pin") is not None:
                _fail(f"{item_path}.pin", "only applies when sensor is 'gpio'")
            roi = Roi.parse(_require(item, "roi", item_path), f"{item_path}.roi")
            threshold = _number(
                _require(item, "threshold", item_path),
                f"{item_path}.threshold",
                minimum=0,
                maximum=255,
            )
            pin = None
        else:
            if item.get("roi") is not None:
                _fail(f"{item_path}.roi", "only applies when sensor is 'camera'")
            roi = None
            # A pin reads 0 or 255, so the threshold is a formality -- but keeping the
            # field means the whole downstream pipeline stays identical.
            threshold = _number(
                item.get("threshold", 90), f"{item_path}.threshold", minimum=0, maximum=255
            )
            pin = _number(
                _require(item, "pin", item_path),
                f"{item_path}.pin",
                integer=True,
                minimum=0,
                maximum=27,
            )
            if pin in pins:
                _fail(f"{item_path}.pin", f"pin {pin} is already used by {pins[pin]!r}")
            pins[pin] = name

        segments.append(
            SegmentConfig(
                name=name,
                roi=roi,
                threshold=threshold,
                pin=pin,
                color=(
                    None
                    if item.get("color") is None
                    else _parse_color(item["color"], f"{item_path}.color")
                ),
            )
        )
    return tuple(segments)


def _parse_gpio(data: Any, path: str) -> GpioConfig:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("sample_hz", "active_low", "pull"), path)
    active_low = _boolean(data.get("active_low", True), f"{path}.active_low")
    pull = _string(data.get("pull", "up"), f"{path}.pull", choices=("up", "down", "none"))
    # The two settings are not independent. An optocoupler that drags the pin low needs
    # the pin resting high, or the pull fights the sensor and the input never changes.
    if pull == "up" and not active_low:
        _fail(f"{path}.pull", "'up' contradicts active_low: false; use pull: down")
    if pull == "down" and active_low:
        _fail(f"{path}.pull", "'down' contradicts active_low: true; use pull: up")
    return GpioConfig(
        sample_hz=_number(data.get("sample_hz", 100.0), f"{path}.sample_hz", minimum=1),
        active_low=active_low,
        pull=pull,
    )


def _parse_dark_reference(data: Any, path: str) -> DarkReferenceConfig | None:
    if data is None:
        return None
    data = _as_mapping(data, path)
    _reject_unknown(data, ("roi", "threshold"), path)
    return DarkReferenceConfig(
        roi=Roi.parse(_require(data, "roi", path), f"{path}.roi"),
        threshold=_number(
            _require(data, "threshold", path), f"{path}.threshold", minimum=0, maximum=255
        ),
    )


def _parse_synthetic(data: Any, path: str, resolution: tuple[int, int]) -> SyntheticConfig:
    data = _as_mapping(data, path)
    allowed = (
        "lit_value",
        "noise_sigma",
        "ambient",
        "glow_px",
        "flash_hz",
        "flash_duty",
        "realtime",
        "seed",
        "segments",
    )
    _reject_unknown(data, allowed, path)

    specs: list[SyntheticSegmentSpec] = []
    raw_segments = data.get("segments")
    if raw_segments is not None:
        if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)):
            _fail(f"{path}.segments", "expected a list")
        for i, raw in enumerate(raw_segments):
            item_path = f"{path}.segments[{i}]"
            item = _as_mapping(raw, item_path)
            _reject_unknown(item, ("name", "roi", "color"), item_path)
            name = _string(_require(item, "name", item_path), f"{item_path}.name")
            roi = Roi.parse(_require(item, "roi", item_path), f"{item_path}.roi")
            roi.check_within(resolution[0], resolution[1], f"{item_path}.roi")
            color = (
                _NAMED_COLORS.get(name.lower(), (255, 255, 255))
                if item.get("color") is None
                else _parse_color(item["color"], f"{item_path}.color")
            )
            specs.append(SyntheticSegmentSpec(name=name, roi=roi, color=color))

    return SyntheticConfig(
        lit_value=_number(
            data.get("lit_value", 210.0), f"{path}.lit_value", minimum=0, maximum=255
        ),
        noise_sigma=_number(data.get("noise_sigma", 1.5), f"{path}.noise_sigma", minimum=0),
        ambient=_number(data.get("ambient", 4.0), f"{path}.ambient", minimum=0, maximum=255),
        glow_px=_number(data.get("glow_px", 0), f"{path}.glow_px", integer=True, minimum=0),
        flash_hz=_number(data.get("flash_hz", 1.5), f"{path}.flash_hz", minimum=0.01),
        flash_duty=_number(data.get("flash_duty", 0.5), f"{path}.flash_duty", minimum=0, maximum=1),
        realtime=_boolean(data.get("realtime", True), f"{path}.realtime"),
        seed=_number(data.get("seed", 0), f"{path}.seed", integer=True, minimum=0),
        segments=tuple(specs),
    )


def _parse_file_source(data: Any, path: str, base_dir: Path | None) -> FileSourceConfig | None:
    if data is None:
        return None
    data = _as_mapping(data, path)
    _reject_unknown(data, ("path", "loop", "pattern", "realtime"), path)
    raw_path = Path(_string(_require(data, "path", path), f"{path}.path")).expanduser()
    if not raw_path.is_absolute() and base_dir is not None:
        raw_path = (base_dir / raw_path).resolve()
    return FileSourceConfig(
        path=raw_path,
        loop=_boolean(data.get("loop", True), f"{path}.loop"),
        pattern=_string(data.get("pattern", "*"), f"{path}.pattern"),
        realtime=_boolean(data.get("realtime", True), f"{path}.realtime"),
    )


def _parse_camera(data: Any, path: str, base_dir: Path | None) -> CameraConfig:
    data = _as_mapping(data, path)
    allowed = (
        "source",
        "resolution",
        "fps",
        "exposure_us",
        "analogue_gain",
        "awb_enable",
        "lens_position",
        "file",
        "synthetic",
        "colour_gains",
        "swap_rb",
        "buffer_count",
    )
    _reject_unknown(data, allowed, path)

    source = _string(_require(data, "source", path), f"{path}.source", choices=CAMERA_SOURCES)
    resolution = _pair(
        _require(data, "resolution", path), f"{path}.resolution", integer=True, minimum=1
    )
    file_cfg = _parse_file_source(data.get("file"), f"{path}.file", base_dir)
    if source == "file" and file_cfg is None:
        _fail(f"{path}.file", "camera.source is 'file' but no camera.file.path is configured")

    return CameraConfig(
        source=source,
        resolution=resolution,
        fps=_number(_require(data, "fps", path), f"{path}.fps", minimum=0.1, maximum=240),
        exposure_us=_number(
            _require(data, "exposure_us", path), f"{path}.exposure_us", integer=True, minimum=1
        ),
        analogue_gain=_number(
            _require(data, "analogue_gain", path), f"{path}.analogue_gain", minimum=0.1
        ),
        awb_enable=_boolean(data.get("awb_enable", False), f"{path}.awb_enable"),
        lens_position=(
            None
            if data.get("lens_position") is None
            else _number(data["lens_position"], f"{path}.lens_position", minimum=0)
        ),
        file=file_cfg,
        synthetic=_parse_synthetic(data.get("synthetic"), f"{path}.synthetic", resolution),
        colour_gains=_pair(
            data.get("colour_gains", [1.0, 1.0]), f"{path}.colour_gains", minimum=0.0
        ),
        swap_rb=_boolean(data.get("swap_rb", True), f"{path}.swap_rb"),
        buffer_count=_number(
            data.get("buffer_count", 4), f"{path}.buffer_count", integer=True, minimum=1
        ),
    )


def _parse_detection(data: Any, path: str) -> DetectionConfig:
    data = _as_mapping(data, path)
    allowed = (
        "debounce_s",
        "flash_window_s",
        "flash_duty_range",
        "flash_min_transitions",
        "metric",
    )
    _reject_unknown(data, allowed, path)

    duty = _pair(
        data.get("flash_duty_range", [0.2, 0.8]),
        f"{path}.flash_duty_range",
        minimum=0.0,
    )
    if not 0.0 <= duty[0] < duty[1] <= 1.0:
        _fail(f"{path}.flash_duty_range", f"expected 0 <= low < high <= 1, got {list(duty)}")

    debounce = _number(data.get("debounce_s", 1.5), f"{path}.debounce_s", minimum=0)
    flash_window = _number(data.get("flash_window_s", 2.0), f"{path}.flash_window_s", minimum=0.1)
    return DetectionConfig(
        debounce_s=debounce,
        flash_window_s=flash_window,
        flash_duty_range=duty,
        flash_min_transitions=_number(
            data.get("flash_min_transitions", 2),
            f"{path}.flash_min_transitions",
            integer=True,
            minimum=1,
        ),
        metric=_string(data.get("metric", "value"), f"{path}.metric", choices=BRIGHTNESS_METRICS),
    )


def _parse_publish(data: Any, path: str) -> PublishConfig:
    data = _as_mapping(data, path)
    allowed = (
        "heartbeat_s",
        "state_refresh_s",
        "snapshot_min_interval_s",
        "snapshot_quality",
        "snapshot_max_width",
        "snapshot_on_transition",
        "snapshot_annotate",
        "snapshot_gain",
    )
    _reject_unknown(data, allowed, path)
    return PublishConfig(
        heartbeat_s=_number(data.get("heartbeat_s", 15.0), f"{path}.heartbeat_s", minimum=1),
        state_refresh_s=_number(
            data.get("state_refresh_s", 60.0), f"{path}.state_refresh_s", minimum=1
        ),
        snapshot_min_interval_s=_number(
            data.get("snapshot_min_interval_s", 300.0), f"{path}.snapshot_min_interval_s", minimum=0
        ),
        snapshot_quality=_number(
            data.get("snapshot_quality", 70),
            f"{path}.snapshot_quality",
            integer=True,
            minimum=1,
            maximum=95,
        ),
        snapshot_max_width=_number(
            data.get("snapshot_max_width", 640),
            f"{path}.snapshot_max_width",
            integer=True,
            minimum=32,
        ),
        snapshot_on_transition=_boolean(
            data.get("snapshot_on_transition", True), f"{path}.snapshot_on_transition"
        ),
        snapshot_annotate=_boolean(
            data.get("snapshot_annotate", True), f"{path}.snapshot_annotate"
        ),
        snapshot_gain=_number(
            data.get("snapshot_gain", 1.0), f"{path}.snapshot_gain", minimum=0.1, maximum=64
        ),
    )


def _parse_states(data: Any, path: str, segment_names: Sequence[str]) -> tuple[StateSpec, ...]:
    data = _as_mapping(data, path)
    if not data:
        _fail(path, "at least one state must be defined")

    specs: list[StateSpec] = []
    for name, raw in data.items():
        item_path = f"{path}.{name}"
        if name == UNKNOWN_STATE:
            _fail(item_path, f"{UNKNOWN_STATE!r} is reserved and cannot be defined")
        if not name.strip():
            _fail(path, "state names must not be empty")

        constraints_raw = _as_mapping(raw, item_path)
        if not constraints_raw:
            _fail(
                item_path,
                "has no segment constraints, so it would match every observation; "
                "name at least one segment",
            )
        _reject_unknown(constraints_raw, segment_names, item_path)

        constraints: dict[str, ModeSet] = {}
        for seg, mode_raw in constraints_raw.items():
            mode_path = f"{item_path}.{seg}"
            if isinstance(mode_raw, bool):
                # YAML 1.1 reads bare on/off/yes/no as booleans, which is exactly the
                # trap this field invites.
                _fail(
                    mode_path,
                    f"got the boolean {mode_raw}. YAML reads unquoted on/off as "
                    'booleans -- write "on" or "off" in quotes',
                )
            token = _string(mode_raw, mode_path, choices=sorted(MODE_TOKENS))
            constraints[seg] = MODE_TOKENS[token]
        specs.append(StateSpec(name=name, constraints=constraints))

    # Two states with an identical constraint set can never be told apart.
    by_constraints: dict[frozenset, list[str]] = {}
    for spec in specs:
        by_constraints.setdefault(frozenset(spec.constraints.items()), []).append(spec.name)
    for names in by_constraints.values():
        if len(names) > 1:
            _fail(path, f"states {names} have identical constraints and can never be distinguished")

    # Two states that tie on specificity for some pattern would be resolved by file
    # order, which is not something anyone should have to reason about.
    for observed, tied in find_ambiguous_patterns(specs, segment_names):
        pattern = ", ".join(f"{k}={v}" for k, v in observed.items())
        _fail(
            path,
            f"states {tied} are equally specific for the pattern ({pattern}); "
            "add a constraint to one of them to break the tie",
        )

    return tuple(specs)


def _parse_logging(data: Any, path: str) -> LoggingConfig:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("level", "format"), path)
    level = _string(data.get("level", "INFO"), f"{path}.level").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        _fail(f"{path}.level", f"unknown log level {level!r}")
    return LoggingConfig(
        level=level,
        format=_string(data.get("format", "text"), f"{path}.format", choices=("text", "json")),
    )


def _parse_calibration(data: Any, path: str, base_dir: Path | None) -> CalibrationConfig:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("host", "port", "sample_dir"), path)
    sample_dir = Path(_string(data.get("sample_dir", "samples"), f"{path}.sample_dir")).expanduser()
    if not sample_dir.is_absolute() and base_dir is not None:
        sample_dir = base_dir / sample_dir
    return CalibrationConfig(
        host=_string(data.get("host", "0.0.0.0"), f"{path}.host"),
        port=_number(
            data.get("port", 8080), f"{path}.port", integer=True, minimum=1, maximum=65535
        ),
        sample_dir=sample_dir,
    )


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------


def parse_config(raw: Any, *, path: Path | None = None, base_dir: Path | None = None) -> NodeConfig:
    """Validate an already-loaded YAML document into a :class:`NodeConfig`."""
    data = _as_mapping(raw, "")
    allowed = (
        "node_id",
        "sensor",
        "mqtt",
        "camera",
        "gpio",
        "segments",
        "dark_reference",
        "detection",
        "publish",
        "states",
        "logging",
        "calibration",
    )
    _reject_unknown(data, allowed, "")

    node_id = _string(_require(data, "node_id", ""), "node_id")
    if not NODE_ID_RE.match(node_id):
        _fail(
            "node_id",
            f"{node_id!r} must be lowercase alphanumeric with '-' or '_' (it becomes an "
            "MQTT topic segment)",
        )

    sensor = _string(data.get("sensor", "camera"), "sensor", choices=SENSORS)
    segments = _parse_segments(_require(data, "segments", ""), "segments", sensor)

    camera: CameraConfig | None = None
    dark_reference: DarkReferenceConfig | None = None
    if sensor == "camera":
        camera = _parse_camera(_require(data, "camera", ""), "camera", base_dir)
        for i, seg in enumerate(segments):
            assert seg.roi is not None  # guaranteed by _parse_segments for this sensor
            seg.roi.check_within(camera.width, camera.height, f"segments[{i}].roi")
        dark_reference = _parse_dark_reference(data.get("dark_reference"), "dark_reference")
        if dark_reference is not None:
            dark_reference.roi.check_within(camera.width, camera.height, "dark_reference.roi")
    else:
        # A GPIO node has no frame, so nothing camera-shaped may be required of it.
        for key in ("camera", "dark_reference"):
            if data.get(key) is not None:
                _fail(key, f"only applies when sensor is 'camera', not {sensor!r}")

    return NodeConfig(
        node_id=node_id,
        sensor=sensor,
        mqtt=_parse_mqtt(data.get("mqtt"), "mqtt"),
        camera=camera,
        gpio=_parse_gpio(data.get("gpio"), "gpio"),
        segments=segments,
        dark_reference=dark_reference,
        detection=_parse_detection(data.get("detection"), "detection"),
        publish=_parse_publish(data.get("publish"), "publish"),
        states=_parse_states(_require(data, "states", ""), "states", [s.name for s in segments]),
        logging=_parse_logging(data.get("logging"), "logging"),
        calibration=_parse_calibration(data.get("calibration"), "calibration", base_dir),
        path=path,
    )


def load_config(path: str | Path) -> NodeConfig:
    """Read and validate a node config file."""
    config_path = Path(path).expanduser()
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    if raw is None:
        raise ConfigError(f"{config_path} is empty")
    return parse_config(raw, path=config_path, base_dir=config_path.parent.resolve())
