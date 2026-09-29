"""MQTT publishing, behind an interface so the node can be tested without a broker.

Same reasoning as :class:`~stacklight.sources.base.FrameSource`: the node runtime talks
to a :class:`Publisher`, so :class:`RecordingPublisher` can stand in and tests can
assert on exactly which topics were published, with what retain and QoS, without
mosquitto running anywhere.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import NodeConfig
from .errors import StackLightError

log = logging.getLogger(__name__)

#: State transitions are worth delivering at least once; heartbeats and snapshots are
#: not, since a fresher one is always along shortly.
QOS_STATE = 1
QOS_TELEMETRY = 0


class PublishError(StackLightError):
    """A publish could not be handed to the broker."""


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    retain: bool = False
    qos: int = QOS_TELEMETRY


class Publisher(ABC):
    """Somewhere to send messages."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def publish(self, topic: str, payload: bytes, *, retain: bool = False, qos: int = 0) -> None:
        """Hand one message over. Raises :class:`PublishError` if it cannot be sent."""

    @property
    def connected(self) -> bool:
        return True

    def __enter__(self) -> Publisher:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()


@dataclass
class RecordingPublisher(Publisher):
    """Keeps every message in a list. For tests and for ``--dry-run``."""

    messages: list[Message] = field(default_factory=list)
    #: Topics that should raise on publish, to exercise the node's failure handling.
    failing_topics: set[str] = field(default_factory=set)
    connect_count: int = 0
    disconnect_count: int = 0

    def connect(self) -> None:
        self.connect_count += 1

    def disconnect(self) -> None:
        self.disconnect_count += 1

    def publish(self, topic: str, payload: bytes, *, retain: bool = False, qos: int = 0) -> None:
        if topic in self.failing_topics:
            raise PublishError(f"publishing to {topic} is set to fail")
        self.messages.append(Message(topic=topic, payload=payload, retain=retain, qos=qos))

    def topics(self) -> list[str]:
        return [message.topic for message in self.messages]

    def on(self, topic: str) -> list[Message]:
        return [message for message in self.messages if message.topic == topic]

    def clear(self) -> None:
        self.messages.clear()


class MqttPublisher(Publisher):
    """paho-mqtt client wired for an always-on node.

    The last will is registered before connecting, so an ungraceful death still flips
    the retained availability topic to ``offline``. paho's own reconnect loop handles
    dropouts; ``on_reconnect`` lets the node resend its state once the link is back,
    since a transition published into a dead socket is simply lost.
    """

    def __init__(
        self,
        config: NodeConfig,
        *,
        will: Message | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._will = will
        self._on_reconnect = on_reconnect
        self._connected = threading.Event()
        self._ever_connected = False
        self._client = self._build_client()

    def _build_client(self):
        # Imported here so `import stacklight` stays cheap and paho stays swappable.
        import paho.mqtt.client as mqtt

        mqtt_config = self._config.mqtt
        client = mqtt.Client(
            # paho 2.x requires an explicit callback API version; VERSION2 is the
            # current signature set and the only one that will keep working.
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=mqtt_config.client_id or f"stacklight-{self._config.node_id}",
        )
        if mqtt_config.username:
            client.username_pw_set(mqtt_config.username, mqtt_config.password)
        if mqtt_config.tls:
            client.tls_set()
        if self._will is not None:
            client.will_set(
                self._will.topic,
                self._will.payload,
                qos=self._will.qos,
                retain=self._will.retain,
            )
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        return client

    # -- paho callbacks (called on the network thread) -----------------------------

    def _handle_connect(self, _client, _userdata, _flags, reason_code, _properties=None) -> None:
        if reason_code != 0:
            log.error("mqtt connection refused", extra={"reason": str(reason_code)})
            return
        was_reconnect = self._ever_connected
        self._ever_connected = True
        self._connected.set()
        log.info(
            "mqtt connected",
            extra={
                "host": self._config.mqtt.host,
                "port": self._config.mqtt.port,
                "reconnect": was_reconnect,
            },
        )
        if was_reconnect and self._on_reconnect is not None:
            try:
                self._on_reconnect()
            except Exception:
                log.exception("on_reconnect handler failed")

    def _handle_disconnect(
        self, _client, _userdata, _flags=None, reason_code=None, _properties=None
    ) -> None:
        self._connected.clear()
        log.warning("mqtt disconnected", extra={"reason": str(reason_code)})

    # -- Publisher -----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self) -> None:
        mqtt_config = self._config.mqtt
        log.info(
            "connecting to mqtt",
            extra={"host": mqtt_config.host, "port": mqtt_config.port},
        )
        try:
            self._client.connect_async(mqtt_config.host, mqtt_config.port, mqtt_config.keepalive)
        except OSError as exc:
            raise PublishError(
                f"cannot reach the broker at {mqtt_config.host}:{mqtt_config.port}: {exc}"
            ) from exc
        # Background thread owns the socket, so a slow or absent broker never stalls
        # the capture loop: publishes fail fast and the retained refresh resyncs later.
        self._client.loop_start()

    def wait_until_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def disconnect(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
        log.info("mqtt client stopped")

    def publish(self, topic: str, payload: bytes, *, retain: bool = False, qos: int = 0) -> None:
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != 0:
            raise PublishError(f"publish to {topic} failed with rc={info.rc}")
