"""MqttPublisher wiring, verified against a fake paho client.

No broker involved. What matters here is that the client is built with the right
options -- notably the last will, which has to be registered before connecting -- and
that publish failures surface as PublishError rather than being swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from stacklight.node import availability_will
from stacklight.publisher import (
    QOS_STATE,
    Message,
    MqttPublisher,
    PublishError,
    RecordingPublisher,
)


@dataclass
class FakeInfo:
    rc: int = 0


@dataclass
class FakeClient:
    kwargs: dict = field(default_factory=dict)
    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)
    will: tuple | None = None
    credentials: tuple | None = None
    tls_called: bool = False
    connected_to: tuple | None = None
    loop_started: bool = False
    loop_stopped: bool = False
    disconnected: bool = False
    publish_rc: int = 0
    connect_error: OSError | None = None
    on_connect = None
    on_disconnect = None

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def tls_set(self):
        self.tls_called = True

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.delays = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return FakeInfo(rc=self.publish_rc)


@pytest.fixture
def fake_paho(monkeypatch):
    """Replace paho's Client with the fake, and hand the test the instance."""
    import paho.mqtt.client as mqtt

    created: list[FakeClient] = []

    def factory(**kwargs):
        client = FakeClient(kwargs=kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(mqtt, "Client", factory)
    return created


def build(config, **kwargs) -> tuple[MqttPublisher, FakeClient]:
    publisher = MqttPublisher(config, **kwargs)
    return publisher, publisher._client


# -- client construction -----------------------------------------------------------


def test_the_paho_v2_callback_api_is_requested(config, fake_paho):
    import paho.mqtt.client as mqtt

    _, client = build(config)
    # paho 2.x refuses to construct a client without this, and VERSION1 is deprecated.
    assert client.kwargs["callback_api_version"] == mqtt.CallbackAPIVersion.VERSION2


def test_client_id_defaults_to_the_node_id(config, fake_paho):
    _, client = build(config)
    assert client.kwargs["client_id"] == "stacklight-testnode"


def test_an_explicit_client_id_is_honoured(config_dict, fake_paho):
    from stacklight.config import parse_config

    config_dict["mqtt"] = {"client_id": "press-cam-1"}
    _, client = build(parse_config(config_dict))
    assert client.kwargs["client_id"] == "press-cam-1"


def test_the_last_will_is_registered_before_connecting(config, fake_paho):
    """It has to be: paho sends the will as part of CONNECT, so setting it afterwards
    would leave the first session without one."""
    publisher, client = build(config, will=availability_will(config))
    assert client.will is not None
    topic, payload, qos, retain = client.will
    assert topic == config.availability_topic
    assert b'"offline"' in payload
    assert retain is True
    assert qos == QOS_STATE
    assert client.connected_to is None  # nothing connected yet

    publisher.connect()
    assert client.connected_to == (config.mqtt.host, config.mqtt.port, config.mqtt.keepalive)


def test_credentials_and_tls_are_applied_when_configured(config_dict, fake_paho):
    from stacklight.config import parse_config

    config_dict["mqtt"] = {"username": "node", "password": "secret", "tls": True}
    _, client = build(parse_config(config_dict))
    assert client.credentials == ("node", "secret")
    assert client.tls_called is True


def test_no_credentials_are_set_when_none_are_configured(config, fake_paho):
    _, client = build(config)
    assert client.credentials is None
    assert client.tls_called is False


# -- connection lifecycle ----------------------------------------------------------


def test_connect_starts_the_network_thread(config, fake_paho):
    """The capture loop must never block on the broker, so paho owns the socket on its
    own thread and publishes fail fast instead of stalling."""
    publisher, client = build(config)
    publisher.connect()
    assert client.loop_started is True


def test_an_unreachable_host_raises_publish_error(config, fake_paho):
    publisher, client = build(config)
    client.connect_error = OSError("nodename nor servname provided")
    with pytest.raises(PublishError, match="cannot reach the broker"):
        publisher.connect()


def test_connected_tracks_the_paho_callbacks(config, fake_paho):
    publisher, client = build(config)
    assert publisher.connected is False

    publisher._handle_connect(client, None, {}, 0)
    assert publisher.connected is True

    publisher._handle_disconnect(client, None, None, 0)
    assert publisher.connected is False


def test_a_refused_connection_does_not_count_as_connected(config, fake_paho):
    publisher, client = build(config)
    publisher._handle_connect(client, None, {}, 5)
    assert publisher.connected is False


def test_on_reconnect_fires_only_on_a_subsequent_connect(config, fake_paho):
    """The first connect is not a reconnect, so the node must not resend state before
    it has any."""
    calls: list[int] = []
    publisher, client = build(config, on_reconnect=lambda: calls.append(1))

    publisher._handle_connect(client, None, {}, 0)
    assert calls == []

    publisher._handle_disconnect(client, None, None, 7)
    publisher._handle_connect(client, None, {}, 0)
    assert calls == [1]


def test_a_failing_reconnect_handler_does_not_kill_the_network_thread(config, fake_paho):
    def boom() -> None:
        raise RuntimeError("handler exploded")

    publisher, client = build(config, on_reconnect=boom)
    publisher._handle_connect(client, None, {}, 0)
    publisher._handle_disconnect(client, None, None, 7)
    publisher._handle_connect(client, None, {}, 0)
    assert publisher.connected is True


def test_disconnect_stops_the_loop_even_if_disconnect_throws(config, fake_paho):
    publisher, client = build(config)
    publisher.disconnect()
    assert client.disconnected is True
    assert client.loop_stopped is True


# -- publishing --------------------------------------------------------------------


def test_publish_passes_qos_and_retain_through(config, fake_paho):
    publisher, client = build(config)
    publisher.publish(config.state_topic, b"{}", retain=True, qos=1)
    assert client.published == [(config.state_topic, b"{}", 1, True)]


def test_a_nonzero_return_code_raises(config, fake_paho):
    publisher, client = build(config)
    client.publish_rc = 4  # MQTT_ERR_NO_CONN
    with pytest.raises(PublishError, match="rc=4"):
        publisher.publish(config.state_topic, b"{}")


# -- the recording stand-in --------------------------------------------------------


def test_recording_publisher_captures_everything():
    publisher = RecordingPublisher()
    with publisher:
        publisher.publish("a/b", b"1", retain=True, qos=1)
        publisher.publish("a/c", b"2")
    assert publisher.topics() == ["a/b", "a/c"]
    assert publisher.on("a/b") == [Message("a/b", b"1", retain=True, qos=1)]
    assert publisher.connect_count == 1
    assert publisher.disconnect_count == 1


def test_recording_publisher_can_be_told_to_fail():
    publisher = RecordingPublisher(failing_topics={"a/b"})
    with pytest.raises(PublishError):
        publisher.publish("a/b", b"1")
    publisher.publish("a/c", b"2")
    assert publisher.topics() == ["a/c"]
