"""Reading the lamp wiring through optocouplers on the Pi's GPIO.

A standalone sensor: no camera, no frames, no calibration. Each lamp lead is tapped in
parallel through an optocoupler, whose output pulls a GPIO pin. The sampler turns those
pins into the same per-segment numbers a camera produces by averaging an ROI --
``LIT`` or ``DARK`` against the same threshold -- so everything downstream is shared.

The pin-reading backend is injected, so the whole sampler is testable on a laptop. The
real one imports ``gpiozero`` lazily, because it only exists on a Pi.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ..clock import Clock, RealClock
from ..config import GpioConfig, NodeConfig
from ..errors import SourceError

log = logging.getLogger(__name__)

#: What an energised lamp lead is worth once it reaches the detector. The pipeline
#: thresholds at 90 either way, so a pin reading sits at the extremes of the same scale
#: a camera's ROI mean moves along.
LIT = 255.0
DARK = 0.0


@dataclass(frozen=True)
class SegmentSample:
    """One poll of every tapped lead."""

    t: float
    wall: datetime
    seq: int
    values: Mapping[str, float]

    def lit(self) -> dict[str, bool]:
        return {name: value > 0 for name, value in self.values.items()}


class PinReader(ABC):
    """Whatever can tell us the level of a pin."""

    @abstractmethod
    def read(self, pin: int) -> bool:
        """True when the pin is at its *active* level, i.e. the lamp lead is energised."""

    def close(self) -> None:  # noqa: B027 -- optional hook; a fake reader has nothing to free
        """Release the pins. Override as needed."""


class FakePinReader(PinReader):
    """In-memory pins, for tests and for ``--dry-run`` on a machine with no GPIO."""

    def __init__(self, pins: Mapping[int, bool] | None = None) -> None:
        self.pins: dict[int, bool] = dict(pins or {})
        self.closed = False
        self.reads = 0

    def read(self, pin: int) -> bool:
        self.reads += 1
        return self.pins.get(pin, False)

    def set(self, pin: int, active: bool) -> None:
        self.pins[pin] = active

    def close(self) -> None:
        self.closed = True


class GpiozeroPinReader(PinReader):
    """The real thing.

    ``gpiozero`` rather than ``RPi.GPIO``: the latter does not work on a Pi 5, where the
    GPIO lives behind the RP1 south bridge and the old ``/dev/gpiomem`` interface is
    gone. Comes from apt as ``python3-gpiozero`` with ``python3-lgpio``, so the venv
    needs ``--system-site-packages`` exactly as it does for ``picamera2``.
    """

    def __init__(self, pins: Sequence[int], *, active_low: bool, pull: str) -> None:
        try:
            from gpiozero import DigitalInputDevice
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise SourceError(
                "gpiozero is not importable. On the Pi: `sudo apt install "
                "python3-gpiozero python3-lgpio`, and create the venv with "
                "`python3 -m venv --system-site-packages` so it can see them."
            ) from exc

        # gpiozero's `pull_up` already encodes polarity: with pull_up=True a pin
        # dragged low reads as active, which is precisely a sinking optocoupler.
        # Applying our own active_low on top of that would invert it a second time and
        # report every segment permanently lit -- which is exactly what it did before
        # this was fixed against real hardware.
        if pull == "none":
            # Nothing holds the pin, so the active level has to be stated outright.
            kwargs: dict[str, object] = {"pull_up": None, "active_state": not active_low}
        else:
            kwargs = {"pull_up": pull == "up"}

        self._devices = {}
        try:
            for pin in pins:
                self._devices[pin] = DigitalInputDevice(pin, **kwargs)
        except Exception as exc:
            self.close()
            raise SourceError(f"could not claim GPIO pins {list(pins)}: {exc}") from exc
        log.info(
            "claimed gpio pins",
            extra={"pins": list(pins), "pull": pull, "active_low": active_low},
        )

    def read(self, pin: int) -> bool:
        # Already "is this input active" -- see the polarity note in __init__.
        return bool(self._devices[pin].value)

    def close(self) -> None:
        for device in self._devices.values():
            try:
                device.close()
            except Exception:  # pragma: no cover - teardown must not mask errors
                log.debug("closing a gpio device raised", exc_info=True)
        self._devices.clear()


class GpioSampler:
    """Polls the tapped leads and yields per-segment values."""

    kind = "gpio"

    def __init__(
        self,
        pins: Mapping[str, int],
        gpio: GpioConfig,
        *,
        reader: PinReader | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not pins:
            raise ValueError("a GPIO sampler needs at least one segment pin")
        self._pins = dict(pins)
        self._gpio = gpio
        self._reader = reader
        self._owns_reader = reader is None
        self._clock = clock or RealClock()
        self._seq = 0
        self._started = False
        self._next_deadline: float | None = None

    @classmethod
    def from_config(
        cls,
        config: NodeConfig,
        *,
        reader: PinReader | None = None,
        clock: Clock | None = None,
    ) -> GpioSampler:
        missing = [seg.name for seg in config.segments if seg.pin is None]
        if missing:
            raise SourceError(f"segment(s) {missing} have no pin; is sensor set to 'gpio'?")
        return cls(
            {seg.name: seg.pin for seg in config.segments},  # type: ignore[misc]
            config.gpio,
            reader=reader,
            clock=clock,
        )

    # -- properties ---------------------------------------------------------------

    @property
    def pins(self) -> dict[str, int]:
        return dict(self._pins)

    @property
    def sample_interval(self) -> float:
        return 1.0 / self._gpio.sample_hz

    @property
    def started(self) -> bool:
        return self._started

    @property
    def clock(self) -> Clock:
        return self._clock

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        if self._reader is None:
            self._reader = GpiozeroPinReader(
                sorted(self._pins.values()),
                active_low=self._gpio.active_low,
                pull=self._gpio.pull,
            )
        self._next_deadline = None
        self._started = True
        log.info(
            "gpio sampler started",
            extra={"pins": self._pins, "sample_hz": self._gpio.sample_hz},
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._owns_reader and self._reader is not None:
            self._reader.close()
            self._reader = None
        log.info("gpio sampler stopped")

    def __enter__(self) -> GpioSampler:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- sampling -----------------------------------------------------------------

    def read(self) -> SegmentSample:
        """Poll every tapped lead once, paced to ``sample_hz``."""
        if not self._started or self._reader is None:
            raise SourceError("GpioSampler.read() called before start()")

        t = self._wait_for_slot()
        values = {
            name: (LIT if self._reader.read(pin) else DARK) for name, pin in self._pins.items()
        }
        self._seq += 1
        return SegmentSample(t=t, wall=datetime.now(UTC), seq=self._seq, values=values)

    def samples(self, limit: int | None = None):
        count = 0
        while limit is None or count < limit:
            yield self.read()
            count += 1

    def _wait_for_slot(self) -> float:
        """Pace polling, and resync rather than chase a backlog after a stall."""
        if self._next_deadline is None:
            self._next_deadline = self._clock.now()
        else:
            self._next_deadline += self.sample_interval
            now = self._clock.now()
            if now - self._next_deadline > self.sample_interval:
                log.debug(
                    "gpio sampling fell behind, resyncing",
                    extra={"behind_s": now - self._next_deadline},
                )
                self._next_deadline = now
        self._clock.sleep_until(self._next_deadline)
        return self._next_deadline

    def __repr__(self) -> str:
        return f"<GpioSampler {self._pins} @ {self._gpio.sample_hz:g}Hz seq={self._seq}>"
