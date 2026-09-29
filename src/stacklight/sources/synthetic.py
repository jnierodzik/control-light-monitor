"""A frame source that draws stack lights from scratch.

This is the load-bearing piece for developing and testing without hardware. It renders
what the real camera is meant to see under heavy underexposure: a near-black frame with
bright rectangles where energised segments are, plus optional sensor noise, ambient
light bleed, and LED glow spill past the ROI edges.

Segments are driven either imperatively (``set_mode``) or by a script of timed steps,
which is how the state machine tests walk the system through sequences like
``off -> steady green -> flashing red -> off``.

Brightness responds to exposure and gain the same way a real sensor does, so the
calibration UI's exposure slider can be demonstrated and tested with no camera.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from ..clock import Clock, RealClock, VirtualClock
from ..config import SegmentConfig, SyntheticConfig, SyntheticSegmentSpec
from ..types import SegmentMode
from .base import CameraControls, FrameSource

log = logging.getLogger(__name__)

#: How bright the LED spill around a segment is, relative to the segment itself.
_GLOW_FRACTION = 0.25


@dataclass(frozen=True)
class ScriptStep:
    """Hold a set of segment modes for ``duration_s`` of source time."""

    duration_s: float
    modes: Mapping[str, SegmentMode]
    #: Per-step flash overrides, so one script can exercise both a plausible 1.5 Hz
    #: blink and a pathological 0.05 duty cycle that must not read as flashing.
    flash_hz: float | None = None
    flash_duty: float | None = None

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError(f"script step duration must be positive, got {self.duration_s}")


@dataclass
class _SegmentState:
    spec: SyntheticSegmentSpec
    mode: SegmentMode = SegmentMode.OFF
    #: Source time at which the current mode began; flashing phase is measured from
    #: here so a flash always starts lit and tests are deterministic.
    phase_origin: float = 0.0
    flash_hz: float | None = None
    flash_duty: float | None = None


class SyntheticSource(FrameSource):
    """Generates frames programmatically. No camera, no files, fully deterministic."""

    kind = "synthetic"

    def __init__(
        self,
        resolution: tuple[int, int],
        fps: float,
        segments: Sequence[SyntheticSegmentSpec],
        *,
        lit_value: float = 210.0,
        noise_sigma: float = 1.5,
        ambient: float = 4.0,
        glow_px: int = 0,
        flash_hz: float = 1.5,
        flash_duty: float = 0.5,
        seed: int = 0,
        controls: CameraControls | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(resolution, fps, controls=controls, clock=clock)
        if not segments:
            raise ValueError("SyntheticSource needs at least one segment to draw")
        if not 0.0 <= flash_duty <= 1.0:
            raise ValueError(f"flash_duty must be in [0, 1], got {flash_duty}")
        if flash_hz <= 0:
            raise ValueError(f"flash_hz must be positive, got {flash_hz}")

        self._segments: dict[str, _SegmentState] = {}
        for spec in segments:
            if spec.name in self._segments:
                raise ValueError(f"duplicate synthetic segment {spec.name!r}")
            spec.roi.check_within(self.width, self.height, f"synthetic segment {spec.name!r}")
            self._segments[spec.name] = _SegmentState(spec=spec)

        self._lit_value = float(lit_value)
        self._noise_sigma = float(noise_sigma)
        self._ambient = float(ambient)
        self._glow_px = int(glow_px)
        self._flash_hz = float(flash_hz)
        self._flash_duty = float(flash_duty)
        self._rng = np.random.default_rng(seed)

        # Exposure and gain are expressed relative to whatever the source was built
        # with, so at the configured settings a lit segment reads exactly lit_value.
        self._nominal_exposure_us = max(1, int(self.controls.exposure_us))
        self._nominal_gain = max(1e-6, float(self.controls.analogue_gain))

        self._script: tuple[ScriptStep, ...] = ()
        self._script_loop = True
        self._script_start: float | None = None
        self._script_finished = False

    # -- classmethods ------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        resolution: tuple[int, int],
        fps: float,
        synthetic: SyntheticConfig,
        detection_segments: Sequence[SegmentConfig] = (),
        *,
        controls: CameraControls | None = None,
        clock: Clock | None = None,
    ) -> SyntheticSource:
        """Build from config, mirroring the detection ROIs when none are given.

        Mirroring is the point: pointing ``camera.source: synthetic`` at a real node
        config replays that exact geometry on a laptop, so ROIs and thresholds can be
        exercised before the hardware exists.
        """
        specs = synthetic.segments
        if not specs:
            specs = tuple(
                SyntheticSegmentSpec(name=seg.name, roi=seg.roi, color=seg.render_color)
                for seg in detection_segments
            )
        if clock is None and not synthetic.realtime:
            clock = VirtualClock()
        return cls(
            resolution,
            fps,
            specs,
            lit_value=synthetic.lit_value,
            noise_sigma=synthetic.noise_sigma,
            ambient=synthetic.ambient,
            glow_px=synthetic.glow_px,
            flash_hz=synthetic.flash_hz,
            flash_duty=synthetic.flash_duty,
            seed=synthetic.seed,
            controls=controls,
            clock=clock or RealClock(),
        )

    # -- segment control ---------------------------------------------------------

    @property
    def segment_names(self) -> tuple[str, ...]:
        return tuple(self._segments)

    def mode_of(self, name: str) -> SegmentMode:
        return self._segments[name].mode

    def set_mode(
        self,
        name: str,
        mode: SegmentMode | str,
        *,
        flash_hz: float | None = None,
        flash_duty: float | None = None,
    ) -> None:
        """Set one segment's behaviour, effective from the next frame."""
        if name not in self._segments:
            raise KeyError(f"unknown synthetic segment {name!r}; have {self.segment_names}")
        state = self._segments[name]
        mode = SegmentMode(mode)
        changed = state.mode is not mode
        state.mode = mode
        state.flash_hz = flash_hz
        state.flash_duty = flash_duty
        if changed:
            state.phase_origin = self._clock.now()

    def set_modes(self, modes: Mapping[str, SegmentMode | str], **kwargs: float | None) -> None:
        for name, mode in modes.items():
            self.set_mode(name, mode, **kwargs)

    def all_off(self) -> None:
        self.set_modes(dict.fromkeys(self._segments, SegmentMode.OFF))

    # -- scripting ---------------------------------------------------------------

    def set_script(self, steps: Iterable[ScriptStep], *, loop: bool = True) -> None:
        """Drive segments from a timeline of steps, starting at the next frame.

        Segments a step omits are turned off, so each step fully describes the pole.
        """
        self._script = tuple(steps)
        self._script_loop = loop
        self._script_start = None
        self._script_finished = False
        for step in self._script:
            unknown = sorted(set(step.modes) - set(self._segments))
            if unknown:
                raise KeyError(f"script references unknown segment(s) {unknown}")

    def clear_script(self) -> None:
        self._script = ()
        self._script_start = None
        self._script_finished = False

    @property
    def script_duration(self) -> float:
        return sum(step.duration_s for step in self._script)

    @property
    def script_finished(self) -> bool:
        """True once a non-looping script has run past its last step."""
        return self._script_finished

    def _apply_script(self, t: float) -> None:
        if not self._script:
            return
        if self._script_start is None:
            self._script_start = t

        elapsed = t - self._script_start
        total = self.script_duration
        if elapsed >= total:
            if not self._script_loop:
                if not self._script_finished:
                    self._script_finished = True
                    log.debug("synthetic script finished", extra={"elapsed_s": elapsed})
                self._activate_step(self._script[-1], self._script_start + total - 1e-9)
                return
            elapsed = elapsed % total

        offset = 0.0
        for step in self._script:
            if elapsed < offset + step.duration_s:
                self._activate_step(step, self._script_start + offset)
                return
            offset += step.duration_s
        last = self._script[-1]
        self._activate_step(last, self._script_start + offset - last.duration_s)

    def _activate_step(self, step: ScriptStep, step_start: float) -> None:
        for name, state in self._segments.items():
            mode = SegmentMode(step.modes.get(name, SegmentMode.OFF))
            if state.mode is not mode or state.phase_origin != step_start:
                state.phase_origin = step_start
            state.mode = mode
            state.flash_hz = step.flash_hz
            state.flash_duty = step.flash_duty

    # -- rendering ---------------------------------------------------------------

    @property
    def exposure_scale(self) -> float:
        """Brightness multiplier from the current exposure and gain.

        A real sensor's response is roughly linear in exposure time times gain until it
        clips, which is close enough to make the calibration slider behave believably.
        """
        return (
            self.controls.exposure_us
            * self.controls.analogue_gain
            / (self._nominal_exposure_us * self._nominal_gain)
        )

    def is_lit(self, name: str, t: float) -> bool:
        """Whether a segment is emitting at source time ``t``. Ground truth for tests."""
        state = self._segments[name]
        if state.mode is SegmentMode.OFF:
            return False
        if state.mode is SegmentMode.STEADY:
            return True
        hz = state.flash_hz if state.flash_hz is not None else self._flash_hz
        duty = state.flash_duty if state.flash_duty is not None else self._flash_duty
        phase = ((t - state.phase_origin) * hz) % 1.0
        return phase < duty

    def _capture(self, t: float) -> np.ndarray:
        self._apply_script(t)
        scale = self.exposure_scale

        canvas = np.zeros((self.height, self.width, 3), dtype=np.float32)
        if self._ambient > 0:
            canvas += self._ambient * scale
        if self._noise_sigma > 0:
            canvas += self._rng.normal(0.0, self._noise_sigma, canvas.shape).astype(np.float32)

        for name, state in self._segments.items():
            if not self.is_lit(name, t):
                continue
            color = np.asarray(state.spec.color, dtype=np.float32) / 255.0
            lit = color * self._lit_value * scale
            roi = state.spec.roi
            rows, cols = roi.slices
            if self._glow_px > 0:
                gx0 = max(0, roi.x - self._glow_px)
                gy0 = max(0, roi.y - self._glow_px)
                gx1 = min(self.width, roi.x + roi.w + self._glow_px)
                gy1 = min(self.height, roi.y + roi.h + self._glow_px)
                # Spill over the whole halo, then take it back off the core, so the
                # ROI itself still reads exactly lit_value rather than 1.25x it.
                canvas[gy0:gy1, gx0:gx1] += lit * _GLOW_FRACTION
                canvas[rows, cols] -= lit * _GLOW_FRACTION
            canvas[rows, cols] += lit

        return np.clip(canvas, 0.0, 255.0).astype(np.uint8)


def off_step(duration_s: float, segments: Iterable[str]) -> ScriptStep:
    """Convenience: every named segment off for ``duration_s``."""
    return ScriptStep(duration_s, dict.fromkeys(segments, SegmentMode.OFF))
