from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from stacklight.config import parse_config
from stacklight.detector import Detector
from stacklight.payloads import (
    availability_payload,
    dumps,
    health_payload,
    iso,
    state_payload,
)
from stacklight.sources import create_source
from stacklight.sources.synthetic import ScriptStep, off_step
from stacklight.types import UNKNOWN_STATE, SegmentMode

from .conftest import at, run_script
from .conftest import states as committed_states  # noqa: F401  (kept for symmetry)

BOTH = ["red", "green"]


def settled_reading(config, modes, *, seconds=8.0):
    readings = run_script(
        config, [off_step(3.0, BOTH), ScriptStep(seconds, modes)], seconds=3.0 + seconds
    )
    return at(readings, 3.0 + seconds - 0.5)


def test_iso_matches_the_documented_shape():
    moment = datetime(2026, 9, 1, 14, 22, 3, 500000, tzinfo=UTC)
    assert iso(moment) == "2026-09-01T14:22:03Z"


def test_state_payload_matches_the_agreed_shape(config):
    reading = settled_reading(config, {"red": SegmentMode.STEADY})
    payload = state_payload(config, reading)

    assert payload["node_id"] == "testnode"
    assert payload["state"] == "fault"
    assert payload["confidence"] == "ok"
    assert set(payload["segments"]) == {"red", "green"}
    assert payload["segments"]["red"]["mode"] == "steady"
    assert payload["segments"]["green"]["mode"] == "off"
    assert payload["since"].endswith("Z")
    # The raw numbers are the point: a threshold cannot be retuned without them.
    assert payload["segments"]["red"]["value"] > 90
    assert payload["segments"]["green"]["value"] < 90
    assert payload["segments"]["red"]["threshold"] == 90.0


def test_state_payload_is_json_serialisable(config):
    reading = settled_reading(config, {"green": SegmentMode.STEADY})
    decoded = json.loads(dumps(state_payload(config, reading)))
    assert decoded["state"] == "running"


def test_state_payload_reports_since_not_publish_time(config):
    """`since` is when the state began; `ts` is when this particular payload was built.

    Note these are wall-clock fields, so under a virtual clock they land in the same
    real second. What is asserted here is the contract: `since` stays pinned to the
    commit across later refreshes, and `ts` always tracks the reading in hand.
    """
    readings = run_script(
        config, [off_step(3.0, BOTH), ScriptStep(8.0, {"green": SegmentMode.STEADY})]
    )
    commit = next(r for r in readings if r.changed and r.state == "running")
    later = at(readings, 10.0)
    assert later.seq > commit.seq

    assert state_payload(config, later)["since"] == state_payload(config, commit)["since"]
    assert state_payload(config, later)["since"] == iso(commit.since)
    assert state_payload(config, later)["ts"] == iso(later.wall)
    assert state_payload(config, later)["seq"] == later.seq


def test_degraded_payload_carries_the_reason_and_withholds_a_verdict(dark_ref_config_dict):
    config = parse_config(dark_ref_config_dict)
    readings = run_script(
        config,
        [
            ScriptStep(5.0, {"green": SegmentMode.STEADY}),
            ScriptStep(6.0, {"green": SegmentMode.STEADY, "stray_light": SegmentMode.STEADY}),
        ],
    )
    payload = state_payload(config, at(readings, 10.0))
    assert payload["state"] == UNKNOWN_STATE
    assert payload["confidence"] == "degraded"
    assert payload["reasons"] == ["dark_reference_bright"]
    # Segment values stay in the payload so the cause is diagnosable remotely.
    assert payload["segments"]["green"]["value"] > 90


def test_health_payload_carries_uptime_fps_and_raw_values(config):
    reading = settled_reading(config, {"green": SegmentMode.STEADY})
    payload = health_payload(config, reading, uptime_s=123.4, source_kind="synthetic")

    assert payload["node_id"] == "testnode"
    assert payload["uptime_s"] == 123.4
    assert payload["fps"] == pytest.approx(config.camera.fps, rel=0.05)
    assert payload["fps_configured"] == config.camera.fps
    assert payload["frames"] == reading.seq
    assert payload["source"] == "synthetic"
    assert payload["state"] == "running"
    assert payload["segments"]["green"]["value"] > 90
    # Detail the lean state payload leaves out, for tuning the flash window.
    assert "transitions" in payload["segments"]["green"]
    assert "duty" in payload["segments"]["green"]
    assert payload["dark_reference"]["value"] < 60


def test_health_payload_handles_a_node_without_a_dark_reference(config_dict):
    del config_dict["dark_reference"]
    config = parse_config(config_dict)
    reading = settled_reading(config, {"green": SegmentMode.STEADY})
    payload = health_payload(config, reading, uptime_s=1.0, source_kind="synthetic")
    assert payload["dark_reference"] is None


def test_health_payload_is_published_before_any_state_commits(config):
    """Warm-up still reports, so a node that never settles is still visible."""
    source = create_source(config, override="synthetic")
    detector = Detector(config)
    with source:
        reading = detector.update(source.read())
    payload = health_payload(config, reading, uptime_s=0.0, source_kind="synthetic")
    assert payload["state"] is None
    assert payload["confidence"] == "degraded"
    assert payload["reasons"] == ["warming_up"]


def test_availability_payload(config):
    assert availability_payload(config, "online") == {"node_id": "testnode", "status": "online"}
