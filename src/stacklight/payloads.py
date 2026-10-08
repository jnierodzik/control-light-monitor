"""Building the JSON published to MQTT.

Kept as pure functions over a :class:`~stacklight.detector.Reading` so payload shape is
testable without a broker, a camera, or a running node.

Raw brightness values travel in every payload. They are the only way to tune a
threshold after the fact, and a node that publishes a verdict without the evidence
behind it cannot be debugged from the outside.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .config import NodeConfig
from .detector import Reading, SegmentReading
from .types import UNKNOWN_STATE

#: Payload for the retained availability topic. The "offline" form is registered with
#: the broker as the node's last will, so it is published on an ungraceful exit too.
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"


def iso(moment: datetime) -> str:
    """UTC timestamp in the ``2026-09-01T14:22:03Z`` form."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(payload: dict[str, Any]) -> bytes:
    """Compact UTF-8 JSON, ready to publish."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _segment(reading: SegmentReading, *, detail: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": reading.mode.value,
        "value": round(reading.value, 1),
        "threshold": round(reading.threshold, 1),
        "lit": reading.lit,
        "duty": round(reading.duty, 3),
    }
    if detail:
        payload["transitions"] = reading.transitions
        payload["settled"] = reading.settled
    return payload


def state_payload(config: NodeConfig, reading: Reading) -> dict[str, Any]:
    """The retained state document.

    ``state`` is never ``None`` here: callers only publish once the detector has
    committed something. A reading the node does not trust publishes ``unknown`` with
    ``confidence: degraded`` rather than a confidently wrong answer.
    """
    return {
        "node_id": config.node_id,
        "state": reading.state or UNKNOWN_STATE,
        "segments": {
            name: _segment(segment, detail=False) for name, segment in reading.segments.items()
        },
        "since": iso(reading.since) if reading.since is not None else iso(reading.wall),
        "confidence": reading.confidence.value,
        "reasons": list(reading.reasons),
        "ts": iso(reading.wall),
        "seq": reading.seq,
    }


def health_payload(
    config: NodeConfig, reading: Reading, *, uptime_s: float, source_kind: str
) -> dict[str, Any]:
    """The heartbeat document.

    Carries the observed frame rate because a node quietly running at 4fps instead of
    15 cannot see a fast flash, and that should be visible without shelling in.
    """
    payload: dict[str, Any] = {
        "node_id": config.node_id,
        "ts": iso(reading.wall),
        "uptime_s": round(uptime_s, 1),
        # None rather than 0.0 on the very first frame: "not measurable yet" is a
        # different thing from "stalled", and the watchdog should not confuse them.
        "fps": round(reading.fps, 2) if reading.fps > 0 else None,
        "fps_configured": config.sample_rate_hz,
        "frames": reading.seq,
        "state": reading.state,
        "candidate_state": reading.candidate_state,
        "confidence": reading.confidence.value,
        "reasons": list(reading.reasons),
        "metric": config.detection.metric,
        "source": source_kind,
        "version": __version__,
        "segments": {
            name: _segment(segment, detail=True) for name, segment in reading.segments.items()
        },
    }
    payload["dark_reference"] = (
        None if reading.dark_reference is None else _segment(reading.dark_reference, detail=True)
    )
    return payload


def availability_payload(config: NodeConfig, status: str) -> dict[str, Any]:
    return {"node_id": config.node_id, "status": status}
