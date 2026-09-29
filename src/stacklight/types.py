"""Vocabulary shared across config, capture, detection and publishing.

These enums are the contract between the detection engine and everything else, so
they live on their own to keep the import graph acyclic.
"""

from __future__ import annotations

from enum import StrEnum


class SegmentMode(StrEnum):
    """How a single stack-light segment is behaving.

    ``FLASHING`` is deliberately not collapsed into ``STEADY``: stack lights
    commonly use flash to mean something different from solid-on.
    """

    OFF = "off"
    STEADY = "steady"
    FLASHING = "flashing"


class Confidence(StrEnum):
    """How much the node trusts its own reading."""

    OK = "ok"
    DEGRADED = "degraded"


#: State published when the observed segment pattern matches no configured state,
#: or when confidence is degraded. Reserved: config may not define a state by this name.
UNKNOWN_STATE = "unknown"

#: Metrics available for reducing an ROI's pixels to a single brightness number.
BRIGHTNESS_METRICS = ("value", "luma", "mean_rgb")
