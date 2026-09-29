"""The node runtime: capture, detect, publish.

One process per camera. It publishes semantic state, never pixels (bar the occasional
debug snapshot), which is what keeps a second camera a deployment concern rather than a
code change.

All scheduling runs off frame timestamps rather than the wall clock, so the whole
runtime -- heartbeat cadence, retained refresh, snapshot rate limiting -- is testable
on a virtual clock in milliseconds.
"""

from __future__ import annotations

import logging

from .config import NodeConfig
from .detector import Detector, Reading
from .payloads import (
    STATUS_OFFLINE,
    STATUS_ONLINE,
    availability_payload,
    dumps,
    health_payload,
    state_payload,
)
from .publisher import QOS_STATE, QOS_TELEMETRY, Message, Publisher, PublishError
from .snapshot import encode_snapshot
from .sources.base import Frame, FrameSource

log = logging.getLogger(__name__)


def availability_will(config: NodeConfig) -> Message:
    """The last will to register with the broker before connecting.

    Depends on nothing but config, so it can be built before the node exists -- which
    it has to be, since paho wants the will set before ``connect()``.
    """
    return Message(
        topic=config.availability_topic,
        payload=dumps(availability_payload(config, STATUS_OFFLINE)),
        retain=True,
        qos=QOS_STATE,
    )


class Node:
    """Runs one camera's capture-detect-publish loop."""

    def __init__(
        self,
        config: NodeConfig,
        source: FrameSource,
        publisher: Publisher,
        *,
        detector: Detector | None = None,
    ) -> None:
        self._config = config
        self._source = source
        self._publisher = publisher
        self._detector = detector or Detector(config)

        self._started_t: float | None = None
        self._last_heartbeat_t: float | None = None
        self._last_state_publish_t: float | None = None
        self._last_snapshot_t: float | None = None
        #: Set when a state publish fails, so the next opportunity retries rather than
        #: waiting out the full refresh interval.
        self._state_pending = False
        self._last_reading: Reading | None = None
        self._stopping = False

    # -- introspection -------------------------------------------------------------

    @property
    def detector(self) -> Detector:
        return self._detector

    @property
    def state(self) -> str | None:
        return self._detector.state

    @property
    def last_reading(self) -> Reading | None:
        return self._last_reading

    def uptime(self, t: float) -> float:
        return 0.0 if self._started_t is None else t - self._started_t

    # -- lifecycle -----------------------------------------------------------------

    def availability_will(self) -> Message:
        return availability_will(self._config)

    def start(self) -> None:
        self._source.start()
        self._publish(
            self._config.availability_topic,
            dumps(availability_payload(self._config, STATUS_ONLINE)),
            retain=True,
            qos=QOS_STATE,
        )
        log.info(
            "node started",
            extra={
                "node_id": self._config.node_id,
                "source": self._source.kind,
                "state_topic": self._config.state_topic,
            },
        )

    def stop(self) -> None:
        """Ask the loop to finish after the current frame."""
        self._stopping = True

    def shutdown(self) -> None:
        """Flip availability to offline and release the camera.

        The last will covers a crash; this covers an orderly systemd stop, where no
        will is sent because the disconnect is clean.
        """
        self._publish(
            self._config.availability_topic,
            dumps(availability_payload(self._config, STATUS_OFFLINE)),
            retain=True,
            qos=QOS_STATE,
        )
        self._source.stop()
        log.info("node stopped", extra={"node_id": self._config.node_id})

    def __enter__(self) -> Node:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()

    # -- the loop ------------------------------------------------------------------

    def run(self, *, max_frames: int | None = None, max_seconds: float | None = None) -> None:
        """Capture and publish until stopped, or until a limit is reached.

        Deliberately does not clear a pending stop: a signal that arrives during
        ``start()``, before the loop begins, must still be honoured.
        """
        frames = 0
        while not self._stopping:
            if max_frames is not None and frames >= max_frames:
                break
            reading = self.step()
            frames += 1
            if max_seconds is not None and self.uptime(reading.t) >= max_seconds:
                break

    def step(self) -> Reading:
        """Capture one frame, run detection, and publish whatever is due."""
        frame = self._source.read()
        reading = self._detector.update(frame)
        self._last_reading = reading
        if self._started_t is None:
            self._started_t = reading.t

        # Three independent schedules. Nesting the snapshot inside the state publish
        # would tie its cadence to state_refresh_s, which is not what it means.
        self._maybe_publish_state(reading)
        self._maybe_publish_snapshot(reading, frame)
        self._maybe_publish_health(reading)
        return reading

    def republish_state(self) -> None:
        """Mark the retained state as needing a resend.

        Wired to the publisher's reconnect callback: a transition published while the
        socket was down is simply gone, and the broker's retained copy is stale until
        something replaces it.
        """
        self._state_pending = True

    # -- publish decisions ---------------------------------------------------------

    def _maybe_publish_state(self, reading: Reading) -> None:
        if reading.state is None:
            # Still warming up. Nothing has been committed, so there is nothing true
            # to say yet.
            return

        due_for_refresh = (
            self._last_state_publish_t is None
            or (reading.t - self._last_state_publish_t) >= self._config.publish.state_refresh_s
        )
        if not (reading.changed or due_for_refresh or self._state_pending):
            return

        payload = dumps(state_payload(self._config, reading))
        try:
            self._publisher.publish(self._config.state_topic, payload, retain=True, qos=QOS_STATE)
        except PublishError as exc:
            self._state_pending = True
            log.warning(
                "state publish failed, will retry",
                extra={"node_id": self._config.node_id, "state": reading.state, "error": str(exc)},
            )
            return

        self._state_pending = False
        self._last_state_publish_t = reading.t
        if reading.changed:
            log.info(
                "published state transition",
                extra={
                    "node_id": self._config.node_id,
                    "state": reading.state,
                    "topic": self._config.state_topic,
                },
            )

    def _maybe_publish_health(self, reading: Reading) -> None:
        interval = self._config.publish.heartbeat_s
        if self._last_heartbeat_t is not None and (reading.t - self._last_heartbeat_t) < interval:
            return
        self._last_heartbeat_t = reading.t
        payload = dumps(
            health_payload(
                self._config,
                reading,
                uptime_s=self.uptime(reading.t),
                source_kind=self._source.kind,
            )
        )
        self._publish(self._config.health_topic, payload, retain=False, qos=QOS_TELEMETRY)

    def _maybe_publish_snapshot(self, reading: Reading, frame: Frame) -> None:
        if reading.changed and self._config.publish.snapshot_on_transition:
            # Bypasses the rate limit on purpose: a notification should carry an image
            # of the state that triggered it, not one from minutes earlier. Debounce
            # already caps transitions at one per debounce_s, so this cannot run away.
            self._publish_snapshot(reading, frame)
            return
        interval = self._config.publish.snapshot_min_interval_s
        if interval <= 0:
            return
        if self._last_snapshot_t is not None and (reading.t - self._last_snapshot_t) < interval:
            return
        self._publish_snapshot(reading, frame)

    def _publish_snapshot(self, reading: Reading, frame: Frame) -> None:
        self._last_snapshot_t = reading.t
        try:
            payload = encode_snapshot(frame, self._config, reading)
        except (OSError, ValueError):
            log.exception("could not encode snapshot", extra={"node_id": self._config.node_id})
            return
        # Retained so a subscriber that starts later -- the rules engine, or a phone
        # notification being built -- always has an image to hand.
        self._publish(self._config.snapshot_topic, payload, retain=True, qos=QOS_TELEMETRY)

    def _publish(self, topic: str, payload: bytes, *, retain: bool, qos: int) -> None:
        """Publish something whose loss is tolerable, logging rather than raising."""
        try:
            self._publisher.publish(topic, payload, retain=retain, qos=qos)
        except PublishError as exc:
            log.warning("publish failed", extra={"topic": topic, "error": str(exc)})
