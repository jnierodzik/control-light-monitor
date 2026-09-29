"""Clock abstraction so timing-dependent code can be tested without sleeping.

Every component that cares about elapsed time takes a :class:`Clock`. Tests inject
:class:`VirtualClock`, which advances instantly, so a 30-second scripted light
sequence runs in milliseconds and is bit-for-bit deterministic.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


class Clock(ABC):
    """A monotonic time source with a sleep primitive."""

    @abstractmethod
    def now(self) -> float:
        """Seconds since an arbitrary epoch. Monotonic, never decreases."""

    @abstractmethod
    def sleep_until(self, deadline: float) -> None:
        """Block (or fast-forward) until :func:`now` is at least ``deadline``."""

    def sleep(self, seconds: float) -> None:
        self.sleep_until(self.now() + seconds)


class RealClock(Clock):
    """Wall-rate clock backed by :func:`time.monotonic`."""

    def now(self) -> float:
        return time.monotonic()

    def sleep_until(self, deadline: float) -> None:
        remaining = deadline - self.now()
        if remaining > 0:
            time.sleep(remaining)


class VirtualClock(Clock):
    """Clock that jumps instantly to any requested deadline.

    Used by tests and by ``FileSource`` replay when running as fast as possible.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def sleep_until(self, deadline: float) -> None:
        if deadline > self._t:
            self._t = float(deadline)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("VirtualClock cannot go backwards")
        self._t += float(seconds)
