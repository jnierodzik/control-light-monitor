"""The ``FrameSource`` interface every capture backend implements.

The detection engine never learns where frames come from. That is what makes the
whole system developable with no camera attached: swap ``PiCameraSource`` for
``SyntheticSource`` and nothing downstream changes.

Two rules keep this honest:

* Frames carry their own timestamp, taken from the source's clock. Downstream code
  must do all elapsed-time maths from ``Frame.t`` and never call ``time.monotonic``
  itself, or tests driven by a virtual clock will not behave like the real thing.
* Camera controls are part of the interface, not of the Pi backend, because the
  calibration UI drives exposure and gain live against whatever source is running.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import numpy as np

from ..clock import Clock, RealClock
from ..errors import SourceError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Frame:
    """One captured frame.

    ``data`` is always ``(height, width, 3)`` uint8 RGB. Keeping colour (rather than
    reducing to grey in the source) costs little and leaves the optional colour sanity
    check and the calibration preview possible.
    """

    data: np.ndarray
    seq: int
    t: float
    wall: datetime

    @property
    def height(self) -> int:
        return int(self.data.shape[0])

    @property
    def width(self) -> int:
        return int(self.data.shape[1])

    @property
    def resolution(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True)
class CameraControls:
    """Manual camera settings.

    The entire detection approach depends on these being *locked*: auto-exposure and
    auto-white-balance would keep re-normalising the frame and undo the deliberate
    underexposure that makes a lit LED the only bright thing in the picture.
    """

    exposure_us: int = 800
    analogue_gain: float = 1.0
    awb_enable: bool = False
    lens_position: float | None = None

    def merged(
        self,
        *,
        exposure_us: int | None = None,
        analogue_gain: float | None = None,
        awb_enable: bool | None = None,
        lens_position: float | None = None,
    ) -> CameraControls:
        changes: dict[str, Any] = {}
        if exposure_us is not None:
            changes["exposure_us"] = int(exposure_us)
        if analogue_gain is not None:
            changes["analogue_gain"] = float(analogue_gain)
        if awb_enable is not None:
            changes["awb_enable"] = bool(awb_enable)
        if lens_position is not None:
            changes["lens_position"] = float(lens_position)
        return replace(self, **changes)


class FrameSource(ABC):
    """A source of frames at a nominal resolution and frame rate."""

    #: Short identifier used in logs and in the ``camera.source`` config value.
    kind: str = "base"

    def __init__(
        self,
        resolution: tuple[int, int],
        fps: float,
        *,
        controls: CameraControls | None = None,
        clock: Clock | None = None,
    ) -> None:
        width, height = int(resolution[0]), int(resolution[1])
        if width <= 0 or height <= 0:
            raise ValueError(f"resolution must be positive, got {resolution!r}")
        if fps <= 0:
            raise ValueError(f"fps must be positive, got {fps!r}")
        self._resolution = (width, height)
        self._fps = float(fps)
        self._controls = controls or CameraControls()
        self._clock = clock or RealClock()
        self._seq = 0
        self._started = False
        self._next_deadline: float | None = None

    # -- properties ---------------------------------------------------------------

    @property
    def resolution(self) -> tuple[int, int]:
        return self._resolution

    @property
    def width(self) -> int:
        return self._resolution[0]

    @property
    def height(self) -> int:
        return self._resolution[1]

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_interval(self) -> float:
        return 1.0 / self._fps

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def controls(self) -> CameraControls:
        return self._controls

    @property
    def started(self) -> bool:
        return self._started

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._next_deadline = None
        self._on_start()
        self._started = True
        log.info(
            "frame source started",
            extra={"source": self.kind, "resolution": self._resolution, "fps": self._fps},
        )

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._on_stop()
        finally:
            self._started = False
        log.info("frame source stopped", extra={"source": self.kind})

    def _on_start(self) -> None:  # noqa: B027 -- optional hook, not every backend needs one
        """Backend-specific startup. Override as needed."""

    def _on_stop(self) -> None:  # noqa: B027 -- optional hook, not every backend needs one
        """Backend-specific teardown. Override as needed."""

    def __enter__(self) -> FrameSource:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- capture ------------------------------------------------------------------

    def read(self) -> Frame:
        """Return the next frame, pacing to ``fps`` via the source's clock."""
        if not self._started:
            raise SourceError(f"{type(self).__name__}.read() called before start()")
        t = self._wait_for_slot()
        data = self._capture(t)
        if data.shape[:2] != (self.height, self.width) or data.shape[2:] != (3,):
            raise SourceError(
                f"{type(self).__name__} produced {data.shape}, expected "
                f"({self.height}, {self.width}, 3)"
            )
        if data.dtype != np.uint8:
            raise SourceError(f"{type(self).__name__} produced {data.dtype}, expected uint8")
        self._seq += 1
        return Frame(data=data, seq=self._seq, t=t, wall=datetime.now(UTC))

    def frames(self, limit: int | None = None) -> Iterator[Frame]:
        """Yield frames until ``limit`` is reached, or forever."""
        count = 0
        while limit is None or count < limit:
            yield self.read()
            count += 1

    def _wait_for_slot(self) -> float:
        """Advance the clock to the next frame slot and return that timestamp.

        With a :class:`RealClock` this sleeps; with a ``VirtualClock`` it jumps, so a
        scripted minute of light behaviour runs instantly in tests.
        """
        if self._next_deadline is None:
            self._next_deadline = self._clock.now()
        else:
            self._next_deadline += self.frame_interval
            # If we have fallen far behind (slow consumer, or a stalled camera), give up
            # on catching up rather than free-running through a backlog of stale slots.
            now = self._clock.now()
            if now - self._next_deadline > self.frame_interval:
                log.debug(
                    "frame pacing fell behind, resyncing",
                    extra={"source": self.kind, "behind_s": now - self._next_deadline},
                )
                self._next_deadline = now
        self._clock.sleep_until(self._next_deadline)
        return self._next_deadline

    @abstractmethod
    def _capture(self, t: float) -> np.ndarray:
        """Produce the frame for source time ``t`` as a ``(H, W, 3)`` uint8 RGB array."""

    # -- controls -----------------------------------------------------------------

    def set_controls(
        self,
        *,
        exposure_us: int | None = None,
        analogue_gain: float | None = None,
        awb_enable: bool | None = None,
        lens_position: float | None = None,
    ) -> CameraControls:
        """Apply camera settings live. Returns the resulting control state.

        Backends that cannot honour a setting (a file replay has no exposure) still
        record it, so the calibration UI has one consistent model to talk to.
        """
        self._controls = self._controls.merged(
            exposure_us=exposure_us,
            analogue_gain=analogue_gain,
            awb_enable=awb_enable,
            lens_position=lens_position,
        )
        self._on_controls_changed(self._controls)
        return self._controls

    def _on_controls_changed(  # noqa: B027 -- optional hook; file replay has no controls
        self, controls: CameraControls
    ) -> None:
        """Push new controls to the backend. Override as needed."""

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} {self.width}x{self.height} @ {self._fps:g}fps seq={self._seq}>"
        )
