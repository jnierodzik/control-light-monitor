from __future__ import annotations

import pytest

from stacklight.clock import RealClock, VirtualClock


def test_virtual_clock_jumps_instead_of_sleeping():
    clock = VirtualClock()
    clock.sleep(3600)
    assert clock.now() == 3600


def test_virtual_clock_never_goes_backwards():
    clock = VirtualClock(10)
    clock.sleep_until(5)
    assert clock.now() == 10
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)


def test_real_clock_is_monotonic():
    clock = RealClock()
    assert clock.now() <= clock.now()
