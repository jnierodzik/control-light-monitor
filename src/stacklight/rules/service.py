"""The rules process: subscribe to node topics, feed the engine, tick it on a timer.

Deliberately thin. All the judgement lives in :class:`~stacklight.rules.engine.
RulesEngine`, which has no network and no clock of its own; this module is the plumbing
that brings it messages and wakes it up periodically.

A malformed payload is logged and dropped rather than raised. The rules process is the
thing that tells you when the machine needs you, so a single bad message must never be
able to take it down.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from .config import RulesConfig
from .engine import RulesEngine
from .notify import Notification

log = logging.getLogger(__name__)

#: How often the time-based rules get a chance to fire. Well under the watchdog's
#: 60-second default, so a missing node is noticed promptly.
TICK_INTERVAL_S = 5.0

KINDS = ("state", "health", "availability")


def parse_topic(topic: str, base: str) -> tuple[str, str] | None:
    """``shop/lights/machine1/state`` -> ``("machine1", "state")``."""
    if not topic.startswith(base + "/"):
        return None
    rest = topic[len(base) + 1 :].split("/")
    if len(rest) != 2 or rest[1] not in KINDS:
        return None
    return rest[0], rest[1]


class RulesService:
    """Runs the engine against a live broker."""

    def __init__(
        self,
        config: RulesConfig,
        engine: RulesEngine,
        *,
        tick_interval_s: float = TICK_INTERVAL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._engine = engine
        self._tick_interval = tick_interval_s
        self._monotonic = monotonic
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=10_000)
        self._stopping = threading.Event()
        self._client: Any | None = None
        self._connected = threading.Event()

    # -- message handling ----------------------------------------------------------

    def handle(self, topic: str, payload: bytes) -> list[Notification]:
        """Dispatch one message. Public so tests can drive the service without a broker."""
        if self._stopping.is_set():
            # We are on our way out, so we are in no position to report that anything
            # else has stopped. This is not hypothetical: systemd restarts both units
            # together, the node publishes `offline` as it exits, and this process --
            # still draining its queue after its own SIGTERM -- would send a watchdog
            # alert one second before stopping too. A planned restart should be quiet.
            #
            # A node that dies on its own is unaffected: this process keeps running,
            # never sets the flag, and reports the outage as it should.
            log.debug("ignoring message during shutdown", extra={"topic": topic})
            return []

        parsed = parse_topic(topic, self._config.mqtt.base_topic)
        if parsed is None:
            return []
        node_id, kind = parsed

        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("dropping unreadable payload", extra={"topic": topic, "error": str(exc)})
            return []
        if not isinstance(document, dict):
            log.warning("dropping non-object payload", extra={"topic": topic})
            return []

        try:
            if kind == "state":
                return self._engine.on_state(node_id, document)
            if kind == "health":
                return self._engine.on_health(node_id, document)
            return self._engine.on_availability(node_id, document)
        except Exception:
            # One bad message must not take down the thing that tells you the machine
            # needs you.
            log.exception("rule evaluation failed", extra={"topic": topic})
            return []

    # -- lifecycle -----------------------------------------------------------------

    def _build_client(self):
        import paho.mqtt.client as mqtt

        mqtt_config = self._config.mqtt
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=mqtt_config.client_id or "stacklight-rules",
        )
        if mqtt_config.username:
            client.username_pw_set(mqtt_config.username, mqtt_config.password)
        if mqtt_config.tls:
            client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if reason_code != 0:
            log.error("mqtt connection refused", extra={"reason": str(reason_code)})
            return
        self._connected.set()
        # Subscribed on every connect, not just the first: a reconnect starts a fresh
        # session and the broker will replay retained state into it.
        for pattern in (
            self._config.state_pattern,
            self._config.health_pattern,
            self._config.availability_pattern,
        ):
            client.subscribe(pattern, qos=1)
        log.info("subscribed", extra={"base_topic": self._config.mqtt.base_topic})

    def _on_disconnect(
        self, _client, _userdata, _flags=None, reason_code=None, _properties=None
    ) -> None:
        self._connected.clear()
        log.warning("mqtt disconnected", extra={"reason": str(reason_code)})

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            self._queue.put_nowait((message.topic, message.payload))
        except queue.Full:
            log.error("message queue full, dropping", extra={"topic": message.topic})

    def connect(self, timeout_s: float = 10.0) -> bool:
        self._client = self._build_client()
        mqtt_config = self._config.mqtt
        log.info("connecting to mqtt", extra={"host": mqtt_config.host, "port": mqtt_config.port})
        self._client.connect_async(mqtt_config.host, mqtt_config.port, mqtt_config.keepalive)
        self._client.loop_start()
        return self._connected.wait(timeout_s)

    def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
        self._client = None

    def stop(self) -> None:
        self._stopping.set()

    # -- the loop ------------------------------------------------------------------

    def run(self, *, max_seconds: float | None = None) -> None:
        """Drain messages and tick the time-based rules until stopped."""
        started = self._monotonic()
        next_tick = started
        while not self._stopping.is_set():
            if max_seconds is not None and self._monotonic() - started >= max_seconds:
                break

            timeout = max(0.05, next_tick - self._monotonic())
            try:
                topic, payload = self._queue.get(timeout=timeout)
            except queue.Empty:
                pass
            else:
                self.handle(topic, payload)

            now = self._monotonic()
            if now >= next_tick:
                self._engine.tick()
                next_tick = now + self._tick_interval

    def drain(self) -> int:
        """Process everything queued right now. For tests and for a clean shutdown."""
        handled = 0
        while True:
            try:
                topic, payload = self._queue.get_nowait()
            except queue.Empty:
                return handled
            self.handle(topic, payload)
            handled += 1
