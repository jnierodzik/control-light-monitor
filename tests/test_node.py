"""Node runtime tests: what gets published, when, with what retain and QoS.

Driven by SyntheticSource on a virtual clock and a RecordingPublisher, so a full hour
of node behaviour is asserted without a broker, a camera, or a second of waiting.
"""

from __future__ import annotations

import json

import pytest

from stacklight.config import parse_config
from stacklight.node import Node, availability_will
from stacklight.publisher import QOS_STATE, QOS_TELEMETRY, RecordingPublisher
from stacklight.sources import create_source
from stacklight.sources.synthetic import ScriptStep, off_step
from stacklight.types import UNKNOWN_STATE, SegmentMode

STEADY, FLASHING = SegmentMode.STEADY, SegmentMode.FLASHING
BOTH = ["red", "green"]


def build(config, script=None, *, loop=False):
    source = create_source(config, override="synthetic")
    if script is not None:
        source.set_script(script, loop=loop)
    publisher = RecordingPublisher()
    return Node(config, source, publisher), publisher


def run(config, script, *, seconds=None, loop=False):
    if seconds is None:
        seconds = sum(step.duration_s for step in script)
    node, publisher = build(config, script, loop=loop)
    with node:
        node.run(max_frames=round(seconds * config.camera.fps))
    return node, publisher


def payloads(publisher, topic):
    return [json.loads(message.payload) for message in publisher.on(topic)]


# -- state topic -------------------------------------------------------------------


def test_state_is_published_on_each_committed_transition(config):
    _, publisher = run(
        config,
        [
            off_step(4.0, BOTH),
            ScriptStep(5.0, {"green": STEADY}),
            ScriptStep(5.0, {"red": FLASHING}),
        ],
    )
    published = [p["state"] for p in payloads(publisher, config.state_topic)]
    assert published == ["idle", "running", "attention"]


def test_state_is_retained_and_sent_at_qos_1(config):
    _, publisher = run(config, [ScriptStep(6.0, {"green": STEADY})])
    messages = publisher.on(config.state_topic)
    assert messages
    assert all(m.retain for m in messages)
    assert all(m.qos == QOS_STATE for m in messages)


def test_nothing_is_published_on_the_state_topic_while_warming_up(config):
    _, publisher = run(config, [ScriptStep(1.5, {"green": STEADY})])
    assert publisher.on(config.state_topic) == []


def test_state_is_not_republished_every_frame(config):
    """Only transitions and the periodic refresh, or the broker would see 15 messages
    a second saying nothing new."""
    _, publisher = run(config, [ScriptStep(30.0, {"green": STEADY})])
    # One commit plus refreshes at the configured interval, not 450 frames' worth.
    assert len(publisher.on(config.state_topic)) <= 3


def test_retained_state_is_refreshed_periodically(config_dict):
    config_dict["publish"] = {"state_refresh_s": 10.0, "heartbeat_s": 15.0}
    config = parse_config(config_dict)
    _, publisher = run(config, [ScriptStep(45.0, {"green": STEADY})])
    published = payloads(publisher, config.state_topic)
    # One commit near t=3, then a refresh every 10s for the rest of the run.
    assert len(published) == 5
    assert {p["state"] for p in published} == {"running"}


def test_an_unconfigured_pattern_publishes_unknown(config):
    _, publisher = run(
        config,
        [off_step(4.0, BOTH), ScriptStep(6.0, {"red": STEADY, "green": STEADY})],
    )
    assert [p["state"] for p in payloads(publisher, config.state_topic)] == [
        "idle",
        UNKNOWN_STATE,
    ]


def test_a_failed_state_publish_is_retried_on_the_next_frame(config):
    node, publisher = build(config, [ScriptStep(8.0, {"green": STEADY})])
    publisher.failing_topics = {config.state_topic}
    with node:
        node.run(max_frames=round(4.0 * config.camera.fps))
        assert publisher.on(config.state_topic) == []
        assert node.state == "running"  # detection carried on regardless

        publisher.failing_topics = set()
        node.run(max_frames=round(1.0 * config.camera.fps))

    published = payloads(publisher, config.state_topic)
    assert [p["state"] for p in published] == ["running"]


def test_republish_state_resends_without_waiting_for_the_refresh(config_dict):
    """What the reconnect callback does: a transition lost to a dead socket leaves the
    broker's retained copy stale, so it is resent as soon as the link is back."""
    config_dict["publish"] = {"state_refresh_s": 3600.0}
    config = parse_config(config_dict)
    node, publisher = build(config, [ScriptStep(30.0, {"green": STEADY})])
    with node:
        node.run(max_frames=round(5.0 * config.camera.fps))
        assert len(publisher.on(config.state_topic)) == 1

        node.republish_state()
        node.run(max_frames=2)
    assert len(publisher.on(config.state_topic)) == 2


# -- health topic ------------------------------------------------------------------


def test_heartbeats_are_published_on_the_configured_interval(config_dict):
    config_dict["publish"] = {"heartbeat_s": 15.0, "state_refresh_s": 3600.0}
    config = parse_config(config_dict)
    _, publisher = run(config, [ScriptStep(62.0, {"green": STEADY})])
    beats = payloads(publisher, config.health_topic)
    # t=0 plus 15, 30, 45, 60.
    assert len(beats) == 5
    assert beats[-1]["uptime_s"] == pytest.approx(60.0, abs=0.2)


def test_heartbeats_start_immediately_not_after_the_first_interval(config):
    """A node that has just come up should say so at once, not in fifteen seconds."""
    _, publisher = run(config, [ScriptStep(1.0, {"green": STEADY})])
    beats = payloads(publisher, config.health_topic)
    assert len(beats) == 1
    assert beats[0]["state"] is None
    assert beats[0]["reasons"] == ["warming_up"]
    # Frame rate is not measurable from a single frame, and null must not be mistaken
    # for a stalled camera by the watchdog.
    assert beats[0]["fps"] is None


def test_heartbeats_carry_raw_brightness_for_every_segment(config_dict):
    config_dict["publish"] = {"heartbeat_s": 2.0}
    config = parse_config(config_dict)
    _, publisher = run(config, [ScriptStep(8.0, {"green": STEADY})])
    beat = payloads(publisher, config.health_topic)[-1]
    assert beat["segments"]["green"]["value"] > 90
    assert beat["segments"]["red"]["value"] < 90
    assert beat["fps"] == pytest.approx(config.camera.fps, rel=0.05)
    assert beat["source"] == "synthetic"


def test_heartbeats_are_fire_and_forget(config):
    _, publisher = run(config, [ScriptStep(8.0, {"green": STEADY})])
    messages = publisher.on(config.health_topic)
    assert all(m.qos == QOS_TELEMETRY and not m.retain for m in messages)


def test_heartbeats_continue_while_degraded(dark_ref_config_dict):
    """The rules engine's watchdog keys off these. A node that goes quiet when it is
    confused would look identical to one that has died."""
    config = parse_config(dark_ref_config_dict)
    _, publisher = run(
        config,
        [
            ScriptStep(5.0, {"green": STEADY}),
            ScriptStep(40.0, {"green": STEADY, "stray_light": STEADY}),
        ],
    )
    beats = payloads(publisher, config.health_topic)
    assert len(beats) >= 3
    assert beats[-1]["confidence"] == "degraded"
    assert beats[-1]["reasons"] == ["dark_reference_bright"]


# -- snapshot topic ----------------------------------------------------------------


def test_a_snapshot_accompanies_every_transition(config):
    """So a notification carries an image of the state that triggered it."""
    _, publisher = run(config, [off_step(4.0, BOTH), ScriptStep(5.0, {"green": STEADY})])
    snapshots = publisher.on(config.snapshot_topic)
    # One at startup, then one per committed transition (idle, running).
    assert len(snapshots) == 3
    assert all(m.payload[:2] == b"\xff\xd8" for m in snapshots)  # JPEG magic
    assert all(m.retain for m in snapshots)


def test_a_snapshot_is_published_at_startup(config):
    """Confirming camera aim on a fresh deploy should not mean waiting for the first
    state change, and the retained image gives any late subscriber something to show."""
    _, publisher = run(config, [ScriptStep(1.0, {"green": STEADY})])
    assert len(publisher.on(config.snapshot_topic)) == 1


def test_snapshots_between_transitions_are_rate_limited(config_dict):
    config_dict["publish"] = {
        "snapshot_min_interval_s": 20.0,
        "state_refresh_s": 3600.0,
        "heartbeat_s": 3600.0,
    }
    config = parse_config(config_dict)
    _, publisher = run(config, [ScriptStep(65.0, {"green": STEADY})])
    # One on the transition, then no more often than every 20s.
    assert 3 <= len(publisher.on(config.snapshot_topic)) <= 5


def test_snapshots_can_be_turned_off_entirely(config_dict):
    config_dict["publish"] = {
        "snapshot_min_interval_s": 0,
        "snapshot_on_transition": False,
    }
    config = parse_config(config_dict)
    _, publisher = run(config, [ScriptStep(30.0, {"green": STEADY})])
    assert publisher.on(config.snapshot_topic) == []


def test_snapshots_are_small_enough_to_push(config):
    _, publisher = run(config, [ScriptStep(6.0, {"green": STEADY})])
    snapshot = publisher.on(config.snapshot_topic)[0]
    assert len(snapshot.payload) < 100_000


# -- availability ------------------------------------------------------------------


def test_availability_flips_online_at_start_and_offline_at_shutdown(config):
    _, publisher = run(config, [ScriptStep(5.0, {"green": STEADY})])
    statuses = [p["status"] for p in payloads(publisher, config.availability_topic)]
    assert statuses == ["online", "offline"]
    assert all(m.retain and m.qos == QOS_STATE for m in publisher.on(config.availability_topic))


def test_the_last_will_says_offline(config):
    will = availability_will(config)
    assert will.topic == config.availability_topic
    assert json.loads(will.payload)["status"] == "offline"
    assert will.retain and will.qos == QOS_STATE


def test_topics_are_all_namespaced_under_the_node_id(config):
    _, publisher = run(config, [ScriptStep(6.0, {"green": STEADY})])
    prefix = f"{config.mqtt.base_topic}/{config.node_id}/"
    assert publisher.messages
    assert all(m.topic.startswith(prefix) for m in publisher.messages)


def test_a_second_node_shares_no_topics_with_the_first(config_dict):
    """Adding a camera is a deployment change: another config file, nothing central."""
    first = parse_config(dict(config_dict, node_id="machine1"))
    second = parse_config(dict(config_dict, node_id="system2"))
    _, first_pub = run(first, [ScriptStep(6.0, {"green": STEADY})])
    _, second_pub = run(second, [ScriptStep(6.0, {"green": STEADY})])
    assert set(first_pub.topics()).isdisjoint(second_pub.topics())


# -- loop mechanics ----------------------------------------------------------------


def test_the_source_is_started_and_stopped_with_the_node(config):
    node, _ = build(config, [ScriptStep(2.0, {"green": STEADY})])
    source = node._source
    assert not source.started
    with node:
        assert source.started
        node.run(max_frames=5)
    assert not source.started


def test_stop_ends_the_loop_after_the_current_frame(config):
    node, _ = build(config, [ScriptStep(60.0, {"green": STEADY})])
    with node:
        node.run(max_frames=10)
        node.stop()
        node.run(max_frames=1000)
    assert node.last_reading is not None
    # The second run() returned at once rather than burning a thousand frames.
    assert node.last_reading.seq <= 12


def test_run_honours_a_seconds_limit(config):
    node, _ = build(config, [ScriptStep(60.0, {"green": STEADY})])
    with node:
        node.run(max_seconds=5.0)
    assert node.last_reading is not None
    assert node.last_reading.t == pytest.approx(5.0, abs=0.1)


def test_a_broker_that_never_works_does_not_stop_detection(config):
    """The camera is the thing that cannot be paused. A node that keeps sampling
    through a broker outage resyncs the moment the link returns."""
    node, publisher = build(config, [ScriptStep(30.0, {"green": STEADY})])
    publisher.failing_topics = {
        config.state_topic,
        config.health_topic,
        config.snapshot_topic,
        config.availability_topic,
    }
    with node:
        node.run(max_frames=round(10.0 * config.camera.fps))
    assert node.state == "running"
    assert publisher.messages == []
