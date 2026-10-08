"""Configuration for the rules engine.

Deliberately a separate file from the node config. The rules process runs somewhere
else, is maintained by a different set of concerns, and should not have to carry a
camera section or an ROI around with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml

from ..config import MqttConfig, _parse_logging, _parse_mqtt
from ..errors import ConfigError
from ..validation import _as_mapping, _boolean, _fail, _number, _reject_unknown, _require, _string

#: ntfy's priority scale, lowest to highest.
PRIORITIES = ("min", "low", "default", "high", "urgent")


def _priority(value: Any, path: str) -> str:
    return _string(value, path, choices=PRIORITIES)


def _time_of_day(value: Any, path: str) -> time:
    text = _string(value, path)
    try:
        hours, _, minutes = text.partition(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        _fail(path, f"expected a 24-hour time like '22:30', got {text!r}")
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class NtfyConfig:
    """Where push notifications go.

    ntfy.sh has no accounts: the topic name is the only secret. Pick something
    unguessable, because anyone who knows it can both read your notifications and send
    convincing fake ones.
    """

    server: str = "https://ntfy.sh"
    topic: str = ""
    timeout_s: float = 10.0
    #: Optional bearer token, for a self-hosted server with access control.
    token: str | None = None


@dataclass(frozen=True)
class QuietHours:
    """A window in which routine notifications are suppressed.

    Crosses midnight when ``start`` is later than ``end``, which is the usual case.
    """

    enabled: bool = False
    start: time = time(22, 0)
    end: time = time(6, 0)
    #: States important enough to wake you anyway.
    override_states: tuple[str, ...] = ("error",)

    def covers(self, moment: time) -> bool:
        if not self.enabled:
            return False
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


@dataclass(frozen=True)
class WatchdogConfig:
    """Notices when a node stops talking.

    A silent monitor is worse than no monitor: without this, a dead node and a quiet
    machine look identical.
    """

    enabled: bool = True
    heartbeat_timeout_s: float = 60.0
    priority: str = "high"
    #: Re-notify this often while a node stays missing, rather than once and forget.
    repeat_s: float = 1800.0


@dataclass(frozen=True)
class RetryConfig:
    """What to do with a notification the network refused to carry.

    Without this a delivery failure is final. That matters here because the machine's
    light is an annunciator: the node publishes ``error`` once, on the transition, and
    then the lamp just stays lit. There is no second transition to trigger a second
    attempt, so one failed POST means the alert is gone for good.

    ``max_age_s`` is the backstop, not the main guard -- a pending alert is normally
    discarded because a newer state superseded it, which is a statement about the
    machine rather than about the clock.
    """

    enabled: bool = True
    #: Minimum gap between attempts for the same notification.
    interval_s: float = 30.0
    #: Give up on a notification older than this.
    max_age_s: float = 1800.0
    #: Cap the queue so a long outage cannot grow it without bound.
    max_pending: int = 32


@dataclass(frozen=True)
class StalenessConfig:
    """Notices when a node is talking but never sees anything happen.

    The tap cannot distinguish a disconnected harness from a machine that is simply
    idle -- both are "all inputs dark" forever. This is the software mitigation: if a
    working day goes by with no light activity at all, say so.
    """

    enabled: bool = False
    after_hours: float = 12.0
    priority: str = "default"
    #: Only count hours inside this window, so an overnight gap is not suspicious.
    between: tuple[time, time] = (time(7, 0), time(19, 0))


@dataclass(frozen=True)
class Rule:
    """What to do when a node commits a particular state."""

    state: str
    notify: bool = True
    title: str = ""
    message: str = ""
    priority: str = "default"
    tags: tuple[str, ...] = ()
    #: Shortest gap between two notifications for this state, per node.
    cooldown_s: float = 0.0
    #: Re-notify at a higher priority if the state is still current after this long.
    escalate_after_s: float | None = None
    escalate_priority: str = "urgent"
    #: Stop escalating after this many reminders. None means keep going.
    escalate_max: int | None = None

    def rendered_title(self, node_id: str) -> str:
        return (self.title or f"{node_id}: {self.state}").replace("{node}", node_id)

    def rendered_message(self, node_id: str) -> str:
        return (self.message or f"State changed to {self.state}").replace("{node}", node_id)


@dataclass(frozen=True)
class RulesConfig:
    mqtt: MqttConfig
    ntfy: NtfyConfig
    database: Path
    rules: tuple[Rule, ...]
    quiet_hours: QuietHours
    watchdog: WatchdogConfig
    staleness: StalenessConfig
    retry: RetryConfig
    logging: Any
    path: Path | None = field(default=None, compare=False)

    def rule_for(self, state: str) -> Rule | None:
        for rule in self.rules:
            if rule.state == state:
                return rule
        return None

    @property
    def state_pattern(self) -> str:
        return f"{self.mqtt.base_topic}/+/state"

    @property
    def health_pattern(self) -> str:
        return f"{self.mqtt.base_topic}/+/health"

    @property
    def availability_pattern(self) -> str:
        return f"{self.mqtt.base_topic}/+/availability"


# -- parsing ---------------------------------------------------------------------


def _parse_ntfy(data: Any, path: str) -> NtfyConfig:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("server", "topic", "timeout_s", "token"), path)
    topic = _string(_require(data, "topic", path), f"{path}.topic")
    if not topic.strip():
        _fail(f"{path}.topic", "must not be empty")
    if "/" in topic:
        _fail(f"{path}.topic", f"is a topic name, not a URL: {topic!r}")
    if len(topic) < 10:
        _fail(
            f"{path}.topic",
            f"{topic!r} is too short to be secret. ntfy has no accounts -- the topic "
            "name is the only thing stopping strangers reading your notifications and "
            "sending fake ones. Use at least 10 characters of something unguessable",
        )
    server = _string(data.get("server", "https://ntfy.sh"), f"{path}.server").rstrip("/")
    if not server.startswith(("http://", "https://")):
        _fail(f"{path}.server", f"must start with http:// or https://, got {server!r}")
    return NtfyConfig(
        server=server,
        topic=topic,
        timeout_s=_number(data.get("timeout_s", 10.0), f"{path}.timeout_s", minimum=0.5),
        token=None if data.get("token") is None else _string(data["token"], f"{path}.token"),
    )


def _parse_quiet_hours(data: Any, path: str) -> QuietHours:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("enabled", "start", "end", "override_states"), path)
    raw_overrides = data.get("override_states", ["error"])
    if not isinstance(raw_overrides, Sequence) or isinstance(raw_overrides, (str, bytes)):
        _fail(f"{path}.override_states", "expected a list of state names")
    return QuietHours(
        enabled=_boolean(data.get("enabled", False), f"{path}.enabled"),
        start=_time_of_day(data.get("start", "22:00"), f"{path}.start"),
        end=_time_of_day(data.get("end", "06:00"), f"{path}.end"),
        override_states=tuple(
            _string(s, f"{path}.override_states[{i}]") for i, s in enumerate(raw_overrides)
        ),
    )


def _parse_watchdog(data: Any, path: str) -> WatchdogConfig:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("enabled", "heartbeat_timeout_s", "priority", "repeat_s"), path)
    return WatchdogConfig(
        enabled=_boolean(data.get("enabled", True), f"{path}.enabled"),
        heartbeat_timeout_s=_number(
            data.get("heartbeat_timeout_s", 60.0), f"{path}.heartbeat_timeout_s", minimum=5
        ),
        priority=_priority(data.get("priority", "high"), f"{path}.priority"),
        repeat_s=_number(data.get("repeat_s", 1800.0), f"{path}.repeat_s", minimum=60),
    )


def _parse_retry(data: Any, path: str) -> RetryConfig:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("enabled", "interval_s", "max_age_s", "max_pending"), path)
    retry = RetryConfig(
        enabled=_boolean(data.get("enabled", True), f"{path}.enabled"),
        interval_s=_number(data.get("interval_s", 30.0), f"{path}.interval_s", minimum=1),
        max_age_s=_number(data.get("max_age_s", 1800.0), f"{path}.max_age_s", minimum=1),
        max_pending=int(_number(data.get("max_pending", 32), f"{path}.max_pending", minimum=1)),
    )
    if retry.interval_s > retry.max_age_s:
        _fail(
            f"{path}.interval_s",
            f"{retry.interval_s}s is longer than max_age_s ({retry.max_age_s}s), so a "
            "queued notification would always age out before it was ever retried",
        )
    return retry


def _parse_staleness(data: Any, path: str) -> StalenessConfig:
    data = _as_mapping(data, path)
    _reject_unknown(data, ("enabled", "after_hours", "priority", "between"), path)
    raw_between = data.get("between", ["07:00", "19:00"])
    if not isinstance(raw_between, Sequence) or len(raw_between) != 2:
        _fail(f"{path}.between", "expected two times, e.g. ['07:00', '19:00']")
    return StalenessConfig(
        enabled=_boolean(data.get("enabled", False), f"{path}.enabled"),
        after_hours=_number(data.get("after_hours", 12.0), f"{path}.after_hours", minimum=0.5),
        priority=_priority(data.get("priority", "default"), f"{path}.priority"),
        between=(
            _time_of_day(raw_between[0], f"{path}.between[0]"),
            _time_of_day(raw_between[1], f"{path}.between[1]"),
        ),
    )


def _parse_rules(data: Any, path: str) -> tuple[Rule, ...]:
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        _fail(path, "expected a list of rules")
    if not data:
        _fail(path, "at least one rule is required, or nothing will ever notify")

    allowed = (
        "state",
        "notify",
        "title",
        "message",
        "priority",
        "tags",
        "cooldown_s",
        "escalate_after_s",
        "escalate_priority",
        "escalate_max",
    )
    rules: list[Rule] = []
    seen: set[str] = set()
    for i, raw in enumerate(data):
        item_path = f"{path}[{i}]"
        item = _as_mapping(raw, item_path)
        _reject_unknown(item, allowed, item_path)
        state = _string(_require(item, "state", item_path), f"{item_path}.state")
        if state in seen:
            _fail(f"{item_path}.state", f"duplicate rule for state {state!r}")
        seen.add(state)

        raw_tags = item.get("tags", [])
        if not isinstance(raw_tags, Sequence) or isinstance(raw_tags, (str, bytes)):
            _fail(f"{item_path}.tags", "expected a list of tag names")

        escalate_after = item.get("escalate_after_s")
        rules.append(
            Rule(
                state=state,
                notify=_boolean(item.get("notify", True), f"{item_path}.notify"),
                title=_string(item.get("title", ""), f"{item_path}.title"),
                message=_string(item.get("message", ""), f"{item_path}.message"),
                priority=_priority(item.get("priority", "default"), f"{item_path}.priority"),
                tags=tuple(_string(t, f"{item_path}.tags[{j}]") for j, t in enumerate(raw_tags)),
                cooldown_s=_number(
                    item.get("cooldown_s", 0.0), f"{item_path}.cooldown_s", minimum=0
                ),
                escalate_after_s=(
                    None
                    if escalate_after is None
                    else _number(escalate_after, f"{item_path}.escalate_after_s", minimum=1)
                ),
                escalate_priority=_priority(
                    item.get("escalate_priority", "urgent"), f"{item_path}.escalate_priority"
                ),
                escalate_max=(
                    None
                    if item.get("escalate_max") is None
                    else _number(
                        item["escalate_max"],
                        f"{item_path}.escalate_max",
                        integer=True,
                        minimum=1,
                    )
                ),
            )
        )
    return tuple(rules)


def parse_rules_config(
    raw: Any, *, path: Path | None = None, base_dir: Path | None = None
) -> RulesConfig:
    data = _as_mapping(raw, "")
    allowed = (
        "mqtt",
        "ntfy",
        "database",
        "rules",
        "quiet_hours",
        "watchdog",
        "staleness",
        "retry",
        "logging",
    )
    _reject_unknown(data, allowed, "")

    database = Path(_string(data.get("database", "history.sqlite"), "database")).expanduser()
    if not database.is_absolute() and base_dir is not None:
        database = base_dir / database

    return RulesConfig(
        mqtt=_parse_mqtt(data.get("mqtt"), "mqtt"),
        ntfy=_parse_ntfy(_require(data, "ntfy", ""), "ntfy"),
        database=database,
        rules=_parse_rules(_require(data, "rules", ""), "rules"),
        quiet_hours=_parse_quiet_hours(data.get("quiet_hours"), "quiet_hours"),
        watchdog=_parse_watchdog(data.get("watchdog"), "watchdog"),
        staleness=_parse_staleness(data.get("staleness"), "staleness"),
        retry=_parse_retry(data.get("retry"), "retry"),
        logging=_parse_logging(data.get("logging"), "logging"),
        path=path,
    )


def load_rules_config(path: str | Path) -> RulesConfig:
    config_path = Path(path).expanduser()
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read rules config {config_path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    if raw is None:
        raise ConfigError(f"{config_path} is empty")
    return parse_rules_config(raw, path=config_path, base_dir=config_path.parent.resolve())
