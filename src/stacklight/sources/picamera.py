"""The real camera, locked to manual control.

This module is imported lazily and never at package import time, because ``picamera2``
only exists on a Raspberry Pi with ``apt install python3-picamera2``. Everything else in
the project runs without it.

Locking the camera is the entire point. Left on automatic, the ISP would keep
re-normalising the frame: auto-exposure would brighten the deliberately black scene until
the room came back, auto-white-balance would shift the hue of a saturated LED
unpredictably, and autofocus would hunt every time the light changed. So exposure time,
analogue gain, colour gains, focus and frame duration are all pinned, and the detector
gets the same frame under the same conditions every time.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from ..clock import Clock
from ..errors import SourceError
from .base import CameraControls, FrameSource

log = logging.getLogger(__name__)


class PiCameraSource(FrameSource):
    """Frames from a Raspberry Pi camera with every automatic behaviour disabled."""

    kind = "picamera"

    def __init__(
        self,
        resolution: tuple[int, int],
        fps: float,
        *,
        controls: CameraControls | None = None,
        clock: Clock | None = None,
        colour_gains: tuple[float, float] = (1.0, 1.0),
        swap_rb: bool = True,
        buffer_count: int = 4,
    ) -> None:
        super().__init__(resolution, fps, controls=controls, clock=clock)
        self._colour_gains = colour_gains
        self._swap_rb = swap_rb
        self._buffer_count = buffer_count
        self._picam: Any | None = None
        self._applied: dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------------

    def _on_start(self) -> None:
        picamera2 = self._import_picamera2()
        frame_duration = round(1_000_000 / self.fps)
        if self.controls.exposure_us > frame_duration:
            log.warning(
                "exposure is longer than the frame interval; the camera will run slower "
                "than configured",
                extra={
                    "exposure_us": self.controls.exposure_us,
                    "frame_duration_us": frame_duration,
                    "fps": self.fps,
                },
            )

        self._picam = picamera2.Picamera2()
        try:
            camera_config = self._picam.create_video_configuration(
                main={"size": self._resolution, "format": "RGB888"},
                buffer_count=self._buffer_count,
                controls=self._control_dict(frame_duration),
            )
            self._picam.configure(camera_config)
            self._picam.start()
        except Exception as exc:
            self._close()
            raise SourceError(f"could not start the Pi camera: {exc}") from exc

        # The sensor mode the ISP settled on decides the field of view, and therefore
        # whether calibrated ROIs still point at the light. Worth having in the log.
        log.info(
            "pi camera started",
            extra={
                "requested": self._resolution,
                "main": self._picam.camera_configuration().get("main", {}).get("size"),
                "sensor": self._picam.camera_configuration().get("sensor", {}),
                "swap_rb": self._swap_rb,
            },
        )
        self._log_applied_controls()

    def _on_stop(self) -> None:
        self._close()

    def _close(self) -> None:
        if self._picam is None:
            return
        try:
            self._picam.stop()
        except Exception:  # pragma: no cover - teardown should never mask an error
            log.debug("pi camera stop() raised during teardown", exc_info=True)
        try:
            self._picam.close()
        except Exception:  # pragma: no cover
            log.debug("pi camera close() raised during teardown", exc_info=True)
        self._picam = None

    @staticmethod
    def _import_picamera2():
        try:
            import picamera2
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise SourceError(
                "picamera2 is not importable. On the Pi: `sudo apt install "
                "python3-picamera2`, and create the venv with "
                "`python3 -m venv --system-site-packages` so it can see it. "
                "On any other machine use camera.source: synthetic."
            ) from exc
        return picamera2

    # -- controls -----------------------------------------------------------------

    def _control_dict(self, frame_duration: int) -> dict[str, Any]:
        """The manual control set. Every automatic behaviour is switched off here."""
        controls: dict[str, Any] = {
            "AeEnable": False,
            "ExposureTime": int(self.controls.exposure_us),
            "AnalogueGain": float(self.controls.analogue_gain),
            "AwbEnable": bool(self.controls.awb_enable),
            # Pinning the frame duration at both ends stops the camera lengthening
            # exposure to hit a target brightness.
            "FrameDurationLimits": (frame_duration, frame_duration),
        }
        if not self.controls.awb_enable:
            # With AWB off the ISP still applies whatever gains it last had, so they
            # have to be stated or the colour balance is undefined.
            controls["ColourGains"] = tuple(float(g) for g in self._colour_gains)

        if self.controls.lens_position is not None:
            af_manual = self._manual_af_mode()
            if af_manual is not None:
                controls["AfMode"] = af_manual
            controls["LensPosition"] = float(self.controls.lens_position)
        else:
            log.warning(
                "no lens_position configured, so focus is left on automatic. The "
                "detector assumes a fixed frame; set camera.lens_position once you "
                "have a value that focuses on the light."
            )
        return controls

    @staticmethod
    def _manual_af_mode() -> Any | None:
        """libcamera's manual-focus enum, or None on a camera without autofocus."""
        try:
            from libcamera import controls as libcamera_controls

            return libcamera_controls.AfModeEnum.Manual
        except (ImportError, AttributeError):  # pragma: no cover - depends on the host
            log.debug("libcamera AfModeEnum unavailable; leaving AfMode alone")
            return None

    def _on_controls_changed(self, controls: CameraControls) -> None:
        """Push new exposure/gain/focus to a running camera.

        This is what makes the calibration UI's sliders work against real hardware.
        """
        if self._picam is None:
            return
        frame_duration = round(1_000_000 / self.fps)
        try:
            self._picam.set_controls(self._control_dict(frame_duration))
        except Exception as exc:
            log.warning("could not apply camera controls", extra={"error": str(exc)})
            return
        log.debug(
            "applied camera controls",
            extra={
                "exposure_us": controls.exposure_us,
                "analogue_gain": controls.analogue_gain,
                "lens_position": controls.lens_position,
            },
        )

    def _log_applied_controls(self) -> None:
        """Report what the camera actually accepted, which is not always what was asked.

        A sensor will silently clamp an exposure outside its supported range, and finding
        that out from a log line beats inferring it from puzzling brightness values.
        """
        if self._picam is None:
            return
        try:
            metadata = self._picam.capture_metadata()
        except Exception:  # pragma: no cover - depends on the host
            log.debug("could not read camera metadata", exc_info=True)
            return
        self._applied = {
            key: metadata.get(key)
            for key in (
                "ExposureTime",
                "AnalogueGain",
                "DigitalGain",
                "ColourGains",
                "LensPosition",
                "FrameDuration",
                "SensorTimestamp",
            )
            if key in metadata
        }
        log.info("pi camera applied controls", extra=dict(self._applied))

    @property
    def applied_controls(self) -> dict[str, Any]:
        """What the camera reported back at start, for the health topic and diagnostics."""
        return dict(self._applied)

    # -- capture ------------------------------------------------------------------

    def _wait_for_slot(self) -> float:
        """Take the sensor's own cadence rather than imposing one.

        ``FrameDurationLimits`` already pins the camera to the configured rate, and
        ``capture_array`` blocks for the next completed frame. Sleeping to a slot on top
        of that would wait out a frame we already have and halve the effective rate.
        """
        return self._clock.now()

    def _capture(self, t: float) -> np.ndarray:
        if self._picam is None:
            raise SourceError("pi camera is not started")
        try:
            data = self._picam.capture_array("main")
        except Exception as exc:
            raise SourceError(f"pi camera capture failed: {exc}") from exc

        if data.ndim != 3 or data.shape[2] < 3:
            raise SourceError(f"pi camera returned an unexpected frame shape {data.shape}")
        if data.shape[2] > 3:
            data = data[:, :, :3]

        # picamera2's format names describe byte layout rather than numpy channel order,
        # so the array from an "RGB888" stream comes out B, G, R. Swapping yields true
        # RGB, which is why swap_rb defaults to true. Detection is indifferent either way
        # -- the default metric takes the max channel -- but snapshots and the optional
        # colour check are not, so this is the escape hatch if colours come out inverted.
        if self._swap_rb:
            data = data[:, :, ::-1]
        return np.ascontiguousarray(data, dtype=np.uint8)
