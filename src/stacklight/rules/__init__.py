"""The rules engine: subscribes to node state, decides what deserves a notification.

Separate from the nodes on purpose. A node's job is to say what the light is doing;
deciding whether that is worth waking someone up is a different concern with different
reasons to change, and keeping them apart is what lets a second node be a deployment
change rather than a code change.
"""

from __future__ import annotations

__all__ = ["RulesConfig", "load_rules_config"]

from .config import RulesConfig, load_rules_config
