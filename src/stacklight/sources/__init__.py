"""Frame sources and the factory that picks one from config."""

from __future__ import annotations

import logging

from ..clock import Clock, VirtualClock
from ..config import NodeConfig
from ..errors import ConfigError, SourceUnavailableError
from .base import CameraControls, Frame, FrameSource
from .file import FileSource
from .synthetic import ScriptStep, SyntheticSource, off_step

log = logging.getLogger(__name__)

__all__ = [
    "CameraControls",
    "FileSource",
    "Frame",
    "FrameSource",
    "ScriptStep",
    "SyntheticSource",
    "controls_from_config",
    "create_source",
    "off_step",
]


def controls_from_config(config: NodeConfig) -> CameraControls:
    """The locked manual camera settings this node runs with."""
    return CameraControls(
        exposure_us=config.camera.exposure_us,
        analogue_gain=config.camera.analogue_gain,
        awb_enable=config.camera.awb_enable,
        lens_position=config.camera.lens_position,
    )


def create_source(
    config: NodeConfig,
    *,
    override: str | None = None,
    clock: Clock | None = None,
) -> FrameSource:
    """Build the frame source named by ``camera.source``.

    ``override`` swaps the backend without editing the file, which is how the same
    production config gets exercised on a laptop (``--source synthetic``).
    """
    camera = config.camera
    kind = override or camera.source
    controls = controls_from_config(config)

    if kind == "synthetic":
        source = SyntheticSource.from_config(
            camera.resolution,
            camera.fps,
            camera.synthetic,
            config.segments,
            controls=controls,
            clock=clock,
        )
    elif kind == "file":
        if camera.file is None:
            raise ConfigError("camera.source is 'file' but camera.file.path is not set")
        if clock is None and not camera.file.realtime:
            clock = VirtualClock()
        source = FileSource(
            camera.file.path,
            camera.resolution,
            camera.fps,
            loop=camera.file.loop,
            pattern=camera.file.pattern,
            controls=controls,
            clock=clock,
        )
    elif kind == "picamera":
        source = _create_picamera_source(config, controls, clock)
    else:
        raise ConfigError(f"unknown camera source {kind!r}")

    log.debug("created frame source", extra={"kind": source.kind, "node_id": config.node_id})
    return source


def _create_picamera_source(
    config: NodeConfig, controls: CameraControls, clock: Clock | None
) -> FrameSource:
    # Imported lazily and by name so this package stays importable on a machine with
    # no picamera2 -- which is every machine except the Pi itself.
    try:
        from .picamera import PiCameraSource
    except ImportError as exc:
        raise SourceUnavailableError(
            "the picamera source needs picamera2, which only exists on a Raspberry "
            "Pi. On the Pi: `sudo apt install python3-picamera2`, and create the venv "
            "with `python3 -m venv --system-site-packages` so it can see it. Anywhere "
            "else use camera.source: synthetic, or pass --source synthetic."
        ) from exc
    return PiCameraSource(
        config.camera.resolution,
        config.camera.fps,
        controls=controls,
        clock=clock,
        colour_gains=config.camera.colour_gains,
        swap_rb=config.camera.swap_rb,
        buffer_count=config.camera.buffer_count,
    )
