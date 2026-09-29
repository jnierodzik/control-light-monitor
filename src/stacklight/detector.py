"""The detection engine: frames in, committed semantic state out.

The pipeline per frame is short on purpose:

1. Sample mean brightness inside each configured ROI (:mod:`stacklight.metrics`).
2. A segment is *lit* if that mean exceeds its threshold.
3. Over a rolling ``flash_window_s``, the lit duty cycle and transition count decide
   whether the segment is ``off``, ``steady`` or ``flashing``.
4. The segment pattern resolves to a configured state name, or to ``unknown``.
5. That state must hold for ``debounce_s`` before it is committed.

Nothing here reads the clock. All timing comes from ``Frame.t``, which is what lets a
virtual clock drive minutes of scripted light behaviour through the engine in
milliseconds.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from .config import DetectionConfig, NodeConfig, resolve_state
from .metrics import measure
from .sources.base import Frame
from .types import UNKNOWN_STATE, Confidence, SegmentMode

log = logging.getLogger(__name__)

#: Reasons a reading is not trusted. Reported in the health payload so a degraded
#: node can be diagnosed without shelling into it.
REASON_WARMING_UP = "warming_up"
REASON_DARK_REFERENCE = "dark_reference_bright"


@dataclass(frozen=True)
class SegmentReading:
    """What one segment looked like on one frame.

    The raw ``value`` is carried all the way through to MQTT deliberately: thresholds
    can only be tuned against the numbers the node actually saw.
    """

    name: str
    mode: SegmentMode
    value: float
    threshold: float
    lit: bool
    duty: float
    transitions: int
    #: False until the rolling window holds a full ``flash_window_s`` of samples, before
    #: which ``mode`` is a guess from partial data.
    settled: bool


@dataclass(frozen=True)
class Reading:
    """The engine's output for one frame."""

    seq: int
    t: float
    wall: datetime
    fps: float
    segments: Mapping[str, SegmentReading]
    dark_reference: SegmentReading | None
    #: The state the current pattern resolves to, before debounce.
    candidate_state: str
    #: The committed state. ``None`` until the first state survives debounce.
    state: str | None
    #: When the committed state began -- the moment its pattern first appeared, not the
    #: moment debounce expired.
    since: datetime | None
    since_t: float | None
    confidence: Confidence
    reasons: tuple[str, ...]
    #: True only on the frame a new state is committed. This is the publish trigger.
    changed: bool

    @property
    def modes(self) -> dict[str, SegmentMode]:
        return {name: reading.mode for name, reading in self.segments.items()}

    @property
    def values(self) -> dict[str, float]:
        return {name: reading.value for name, reading in self.segments.items()}

    @property
    def degraded(self) -> bool:
        return self.confidence is Confidence.DEGRADED


class SegmentTracker:
    """Classifies one region as off / steady / flashing from a rolling window.

    Used for the light segments and, unchanged, for the dark reference: a region that
    must always read dark is just a segment whose mode is expected to stay ``off``.
    """

    def __init__(self, name: str, threshold: float, detection: DetectionConfig) -> None:
        self._name = name
        self._threshold = float(threshold)
        self._window_s = detection.flash_window_s
        self._duty_low, self._duty_high = detection.flash_duty_range
        self._min_transitions = detection.flash_min_transitions
        self._samples: deque[tuple[float, bool]] = deque()
        self._first_t: float | None = None
        self._mode = SegmentMode.OFF
        self._mode_known = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def mode(self) -> SegmentMode:
        return self._mode

    def reset(self) -> None:
        self._samples.clear()
        self._first_t = None
        self._mode = SegmentMode.OFF
        self._mode_known = False

    def update(self, t: float, value: float) -> SegmentReading:
        lit = value > self._threshold
        if self._first_t is None:
            self._first_t = t
        self._samples.append((t, lit))
        cutoff = t - self._window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        flags = [sample_lit for _, sample_lit in self._samples]
        duty = sum(flags) / len(flags)
        transitions = sum(a != b for a, b in pairwise(flags))
        settled = (t - self._first_t) >= self._window_s

        mode = self._classify(duty, transitions)
        self._mode = mode
        self._mode_known = True

        return SegmentReading(
            name=self._name,
            mode=mode,
            value=value,
            threshold=self._threshold,
            lit=lit,
            duty=duty,
            transitions=transitions,
            settled=settled,
        )

    def _classify(self, duty: float, transitions: int) -> SegmentMode:
        """Turn a duty cycle and transition count into a mode.

        The duty range does double duty: inside it (with enough transitions) means
        flashing, above it means steady, below it means off. What is left -- a duty
        mid-range with too few transitions -- is genuinely ambiguous, and happens
        while the window is still draining after a real change. There the previous
        mode is held rather than guessed at, so a segment coming on reads ``off``
        until the evidence is solid instead of flickering on the way up.
        """
        if transitions >= self._min_transitions and self._duty_low <= duty <= self._duty_high:
            return SegmentMode.FLASHING
        if duty >= self._duty_high:
            return SegmentMode.STEADY
        if duty <= self._duty_low:
            return SegmentMode.OFF
        if self._mode_known:
            return self._mode
        return SegmentMode.STEADY if duty >= 0.5 else SegmentMode.OFF


class Detector:
    """Drives frames through sampling, mode classification, and debounce."""

    def __init__(self, config: NodeConfig) -> None:
        self._config = config
        self._metric = config.detection.metric
        self._debounce_s = config.detection.debounce_s
        self._trackers = {
            seg.name: SegmentTracker(seg.name, seg.threshold, config.detection)
            for seg in config.segments
        }
        self._dark_tracker = (
            SegmentTracker("dark_reference", config.dark_reference.threshold, config.detection)
            if config.dark_reference is not None
            else None
        )
        self._fps_samples: deque[float] = deque()
        self._fps_window_s = max(1.0, config.detection.flash_window_s)

        self._candidate: str | None = None
        self._candidate_since_t: float = 0.0
        self._candidate_since_wall: datetime | None = None
        self._state: str | None = None
        self._since_t: float | None = None
        self._since_wall: datetime | None = None

    # -- introspection -------------------------------------------------------------

    @property
    def config(self) -> NodeConfig:
        return self._config

    @property
    def state(self) -> str | None:
        """The committed state, or ``None`` before the first one settles."""
        return self._state

    @property
    def since(self) -> datetime | None:
        return self._since_wall

    def reset(self) -> None:
        for tracker in self._trackers.values():
            tracker.reset()
        if self._dark_tracker is not None:
            self._dark_tracker.reset()
        self._fps_samples.clear()
        self._candidate = None
        self._candidate_since_wall = None
        self._state = None
        self._since_t = None
        self._since_wall = None

    # -- the pipeline --------------------------------------------------------------

    def update(self, frame: Frame) -> Reading:
        """Feed one frame through the engine and return the resulting reading."""
        t = frame.t
        segments = {
            seg.name: self._trackers[seg.name].update(t, measure(frame.data, seg.roi, self._metric))
            for seg in self._config.segments
        }

        dark: SegmentReading | None = None
        if self._dark_tracker is not None and self._config.dark_reference is not None:
            dark = self._dark_tracker.update(
                t, measure(frame.data, self._config.dark_reference.roi, self._metric)
            )

        warming_up = not all(reading.settled for reading in segments.values())
        if dark is not None and not dark.settled:
            warming_up = True
        # A single bright frame is a reflection or a passing torch; the dark reference
        # only trips when it stays bright, which is what a bumped camera looks like.
        dark_tripped = dark is not None and dark.settled and dark.mode is not SegmentMode.OFF

        reasons: list[str] = []
        if warming_up:
            reasons.append(REASON_WARMING_UP)
        if dark_tripped:
            reasons.append(REASON_DARK_REFERENCE)

        confidence = Confidence.DEGRADED if reasons else Confidence.OK
        # A degraded reading never resolves to a named state: a confidently wrong
        # answer is worse than admitting we do not know.
        candidate = (
            UNKNOWN_STATE
            if reasons
            else resolve_state(self._config.states, {name: r.mode for name, r in segments.items()})
        )

        if candidate != self._candidate:
            self._candidate = candidate
            self._candidate_since_t = t
            self._candidate_since_wall = frame.wall

        changed = self._maybe_commit(candidate, t, warming_up, segments, dark)

        return Reading(
            seq=frame.seq,
            t=t,
            wall=frame.wall,
            fps=self._estimate_fps(t),
            segments=segments,
            dark_reference=dark,
            candidate_state=candidate,
            state=self._state,
            since=self._since_wall,
            since_t=self._since_t,
            confidence=confidence,
            reasons=tuple(reasons),
            changed=changed,
        )

    def _maybe_commit(
        self,
        candidate: str,
        t: float,
        warming_up: bool,
        segments: Mapping[str, SegmentReading],
        dark: SegmentReading | None,
    ) -> bool:
        # Nothing commits while the window is filling. Otherwise every boot would
        # publish a spurious "unknown" that means only "started up a moment ago".
        if warming_up or candidate == self._state:
            return False
        if (t - self._candidate_since_t) < self._debounce_s:
            return False

        previous = self._state
        self._state = candidate
        self._since_t = self._candidate_since_t
        self._since_wall = self._candidate_since_wall
        log.info(
            "state committed",
            extra={
                "node_id": self._config.node_id,
                "state": candidate,
                "previous": previous,
                "held_s": round(t - self._candidate_since_t, 3),
                "modes": {name: r.mode.value for name, r in segments.items()},
                "values": {name: round(r.value, 1) for name, r in segments.items()},
                "dark_reference": None if dark is None else round(dark.value, 1),
            },
        )
        return True

    def _estimate_fps(self, t: float) -> float:
        """Observed frame rate, for the health topic.

        Reported rather than assumed: a node quietly running at 4fps instead of 15
        cannot detect a fast flash, and that should be visible from the outside.
        """
        self._fps_samples.append(t)
        cutoff = t - self._fps_window_s
        while len(self._fps_samples) > 2 and self._fps_samples[0] < cutoff:
            self._fps_samples.popleft()
        span = self._fps_samples[-1] - self._fps_samples[0]
        if span <= 0:
            return 0.0
        return (len(self._fps_samples) - 1) / span


def detection_latency_bounds(detection: DetectionConfig) -> tuple[float, float]:
    """Rough best and worst case delay between a light changing and a commit.

    Useful for sanity-checking a config: the window has to drain before a segment's
    duty cycle clears the steady or off boundary, and only then does debounce start.
    """
    _, high = detection.flash_duty_range
    best = detection.flash_window_s * (1.0 - high) + detection.debounce_s
    worst = detection.flash_window_s * high + detection.debounce_s
    return best, worst
