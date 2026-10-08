"""Deciding what deserves a notification.

Pure logic: payloads and a clock in, notifications out. No MQTT, no HTTP, no sleeping.
That is what makes "does it spam me when the light flaps" a test rather than an
afternoon of watching a phone.

The clock is injected as a callable returning an aware datetime, because every rule
here is about wall-clock time -- quiet hours, escalation after N minutes, a working day
with no activity. Monotonic time cannot express any of that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Rule, RulesConfig
from .notify import Notification, Notifier, NotifyError
from .store import History

log = logging.getLogger(__name__)

KIND_STATE = "state"
KIND_WATCHDOG = "watchdog"
KIND_RECOVERED = "recovered"
KIND_STALE = "staleness"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _format_delay(seconds: float) -> str:
    """A human-readable lateness, for a notification that did not go out on time."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


@dataclass
class PendingNotification:
    """A notification the network would not carry, waiting for another attempt."""

    notification: Notification
    node_id: str
    first_attempt: datetime
    last_attempt: datetime
    attempts: int = 1
    #: The state whose cooldown this notification claims, once it is actually sent.
    mark_state: str | None = None

    def age_s(self, now: datetime) -> float:
        return (now - self.first_attempt).total_seconds()


@dataclass
class NodeTracker:
    """What the engine remembers about one node."""

    node_id: str
    state: str | None = None
    #: When the current state was committed, by our clock rather than the node's.
    state_since: datetime | None = None
    last_heartbeat: datetime | None = None
    last_activity: datetime | None = None
    #: Last notification per state, for cooldown.
    last_notified: dict[str, datetime] = field(default_factory=dict)
    escalations: int = 0
    last_escalation: datetime | None = None
    missing: bool = False
    last_missing_notice: datetime | None = None
    stale_notified: bool = False


class RulesEngine:
    """Turns node payloads into notifications, and logs everything to history."""

    def __init__(
        self,
        config: RulesConfig,
        notifier: Notifier,
        history: History,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._history = history
        self._now = now
        self._nodes: dict[str, NodeTracker] = {}
        self._pending: list[PendingNotification] = []

    # -- introspection -------------------------------------------------------------

    @property
    def nodes(self) -> Mapping[str, NodeTracker]:
        return dict(self._nodes)

    @property
    def pending(self) -> tuple[PendingNotification, ...]:
        """Notifications that failed to send and are waiting for another attempt."""
        return tuple(self._pending)

    def tracker(self, node_id: str) -> NodeTracker:
        if node_id not in self._nodes:
            self._nodes[node_id] = NodeTracker(node_id=node_id)
        return self._nodes[node_id]

    # -- inputs --------------------------------------------------------------------

    def on_state(self, node_id: str, payload: Mapping[str, Any]) -> list[Notification]:
        """A retained state message arrived."""
        state = str(payload.get("state", "unknown"))
        tracker = self.tracker(node_id)
        now = self._now()

        if state == tracker.state:
            # A periodic refresh of the same state, not a transition. Nothing to log and
            # nothing to announce -- the node republishes on a timer.
            return []

        previous = tracker.state
        self._history.record_transition(node_id, payload, previous)
        tracker.state = state
        tracker.state_since = now
        tracker.escalations = 0
        tracker.last_escalation = None
        tracker.last_activity = now
        tracker.stale_notified = False

        rule = self._config.rule_for(state)
        if rule is None or not rule.notify:
            log.debug("state not notifiable", extra={"node_id": node_id, "state": state})
            return []

        if self._in_cooldown(tracker, rule, now):
            log.info(
                "notification suppressed by cooldown",
                extra={"node_id": node_id, "state": state, "cooldown_s": rule.cooldown_s},
            )
            return []

        if self._suppressed_by_quiet_hours(state, now):
            log.info(
                "notification suppressed by quiet hours", extra={"node_id": node_id, "state": state}
            )
            return []

        return self._emit(
            Notification(
                title=rule.rendered_title(node_id),
                message=rule.rendered_message(node_id),
                priority=rule.priority,
                tags=rule.tags,
                kind=KIND_STATE,
                node_id=node_id,
                state=state,
            ),
            tracker,
            now,
            mark_state=state,
        )

    def on_health(self, node_id: str, payload: Mapping[str, Any]) -> list[Notification]:
        """A heartbeat arrived. Mostly this just proves the node is alive."""
        tracker = self.tracker(node_id)
        now = self._now()
        tracker.last_heartbeat = now
        if tracker.last_activity is None:
            tracker.last_activity = now

        if tracker.missing:
            tracker.missing = False
            tracker.last_missing_notice = None
            return self._emit(
                Notification(
                    title=f"{node_id}: monitor back",
                    message="Heartbeats have resumed.",
                    priority="default",
                    tags=("white_check_mark",),
                    kind=KIND_RECOVERED,
                    node_id=node_id,
                ),
                tracker,
                now,
            )
        return []

    def on_availability(self, node_id: str, payload: Mapping[str, Any]) -> list[Notification]:
        """The retained availability flag changed.

        An ``offline`` here is the fast path: the broker noticed the socket die and
        published the node's last will, which beats waiting out the heartbeat timeout.
        """
        tracker = self.tracker(node_id)
        now = self._now()
        if str(payload.get("status", "")) != "offline" or tracker.missing:
            return []
        if not self._config.watchdog.enabled:
            return []

        tracker.missing = True
        tracker.last_missing_notice = now
        return self._emit(
            Notification(
                title=f"{node_id}: monitor offline",
                message="The node disconnected from the broker. Nothing is watching the light.",
                priority=self._config.watchdog.priority,
                tags=("warning",),
                kind=KIND_WATCHDOG,
                node_id=node_id,
            ),
            tracker,
            now,
        )

    def tick(self) -> list[Notification]:
        """Time-based rules: escalation, the heartbeat watchdog, and staleness.

        Called on a timer by the service. Everything here is a thing that happens
        because of elapsed time rather than because a message arrived.
        """
        now = self._now()
        # Retries come first: a backlog is older than anything we are about to decide,
        # and draining it keeps a recovered network from reordering the alerts.
        out: list[Notification] = self._retry_pending(now)
        for tracker in list(self._nodes.values()):
            out += self._check_escalation(tracker, now)
            out += self._check_watchdog(tracker, now)
            out += self._check_staleness(tracker, now)
        return out

    # -- redelivery ----------------------------------------------------------------

    def _superseded_by(self, item: PendingNotification) -> str | None:
        """Why this pending notification no longer describes the machine, if it doesn't.

        Age is a poor reason to drop an alert -- a latched red lamp is just as true ten
        minutes late. Being overtaken by reality is a good one.
        """
        tracker = self._nodes.get(item.node_id)
        if tracker is None:
            return None
        kind = item.notification.kind
        if (
            kind == KIND_STATE
            and item.mark_state is not None
            and tracker.state is not None
            and tracker.state != item.mark_state
        ):
            return f"state moved on to '{tracker.state}'"
        if kind == KIND_WATCHDOG and not tracker.missing:
            return "node came back before the alert could be sent"
        return None

    def _retry_pending(self, now: datetime) -> list[Notification]:
        """Try the queue again, and drop whatever is no longer worth sending."""
        if not self._pending:
            return []
        retry = self._config.retry
        sent: list[Notification] = []
        for item in list(self._pending):
            reason = self._superseded_by(item)
            if reason is None and item.age_s(now) > retry.max_age_s:
                reason = f"gave up after {_format_delay(item.age_s(now))} and {item.attempts} tries"
            if reason is not None:
                self._pending.remove(item)
                self._record_attempt(item, now, delivered=False, error=reason)
                log.warning(
                    "notification abandoned",
                    extra={
                        "node_id": item.node_id,
                        "title": item.notification.title,
                        "reason": reason,
                        "attempts": item.attempts,
                    },
                )
                continue

            if (now - item.last_attempt).total_seconds() < retry.interval_s:
                continue

            item.attempts += 1
            item.last_attempt = now
            # Say that it is late. An alert delivered ten minutes after the fact is
            # still worth having, but not if it reads as having just happened.
            late = replace(
                item.notification,
                message=(
                    f"{item.notification.message} "
                    f"(delayed {_format_delay(item.age_s(now))}; "
                    f"attempt {item.attempts})"
                ),
            )
            try:
                self._notifier.send(late)
            except NotifyError as exc:
                log.warning(
                    "notification retry failed",
                    extra={
                        "node_id": item.node_id,
                        "title": item.notification.title,
                        "attempts": item.attempts,
                        "error": str(exc),
                    },
                )
                continue

            self._pending.remove(item)
            if item.mark_state is not None:
                self.tracker(item.node_id).last_notified[item.mark_state] = now
            self._record_attempt(item, now, delivered=True, error=None, notification=late)
            log.info(
                "notification delivered on retry",
                extra={
                    "node_id": item.node_id,
                    "title": item.notification.title,
                    "attempts": item.attempts,
                    "delayed_s": round(item.age_s(now), 1),
                },
            )
            sent.append(late)
        return sent

    def _record_attempt(
        self,
        item: PendingNotification,
        now: datetime,
        *,
        delivered: bool,
        error: str | None,
        notification: Notification | None = None,
    ) -> None:
        """Write the terminal outcome of a queued notification.

        Only the first failure and the final outcome are recorded. The attempts in
        between are in the log, not the database -- a long outage should not write a row
        every thirty seconds for an alert nobody has seen yet.
        """
        n = notification or item.notification
        self._history.record_notification(
            node_id=item.node_id,
            kind=n.kind,
            state=n.state,
            ts=now,
            priority=n.priority,
            title=n.title,
            message=n.message,
            escalation=n.escalation,
            delivered=delivered,
            error=error,
            attempt=item.attempts,
        )

    def _enqueue(
        self,
        notification: Notification,
        tracker: NodeTracker,
        now: datetime,
        mark_state: str | None,
    ) -> None:
        """Hold a failed notification for another attempt on a later tick."""
        retry = self._config.retry
        if not retry.enabled:
            return
        node_id = notification.node_id or tracker.node_id
        # Collapse repeats. A light flapping error -> clear -> error through an outage
        # would otherwise queue an alert per transition and deliver the lot in a burst
        # the moment the network came back. One alert per node, kind and state says the
        # same thing without the pile-up. The first attempt time is carried over, so
        # repeating does not let a notification outlive max_age_s.
        duplicate = next(
            (
                item
                for item in self._pending
                if item.node_id == node_id
                and item.notification.kind == notification.kind
                and item.notification.state == notification.state
                and item.notification.escalation == notification.escalation
            ),
            None,
        )
        if duplicate is not None:
            duplicate.notification = notification
            duplicate.mark_state = mark_state
            return
        self._pending.append(
            PendingNotification(
                notification=notification,
                node_id=node_id,
                first_attempt=now,
                last_attempt=now,
                mark_state=mark_state,
            )
        )
        # Oldest first: during a long outage the newest alerts describe the machine as
        # it is now, so those are the ones worth keeping.
        while len(self._pending) > retry.max_pending:
            dropped = self._pending.pop(0)
            log.warning(
                "notification dropped, retry queue full",
                extra={
                    "node_id": dropped.node_id,
                    "title": dropped.notification.title,
                    "max_pending": retry.max_pending,
                },
            )

    # -- time-based rules ----------------------------------------------------------

    def _check_escalation(self, tracker: NodeTracker, now: datetime) -> list[Notification]:
        if tracker.state is None or tracker.state_since is None:
            return []
        rule = self._config.rule_for(tracker.state)
        if rule is None or not rule.notify or rule.escalate_after_s is None:
            return []
        if rule.escalate_max is not None and tracker.escalations >= rule.escalate_max:
            return []

        last = tracker.last_escalation or tracker.state_since
        if (now - last).total_seconds() < rule.escalate_after_s:
            return []

        tracker.escalations += 1
        tracker.last_escalation = now
        waited = int((now - tracker.state_since).total_seconds() // 60)
        return self._emit(
            Notification(
                title=f"Still {tracker.state}: {tracker.node_id}",
                message=f"{rule.rendered_message(tracker.node_id)} ({waited} min and counting)",
                priority=rule.escalate_priority,
                tags=rule.tags,
                kind=KIND_STATE,
                node_id=tracker.node_id,
                state=tracker.state,
                escalation=tracker.escalations,
            ),
            tracker,
            now,
        )

    def _check_watchdog(self, tracker: NodeTracker, now: datetime) -> list[Notification]:
        watchdog = self._config.watchdog
        if not watchdog.enabled or tracker.last_heartbeat is None:
            return []
        silent_for = (now - tracker.last_heartbeat).total_seconds()
        if silent_for < watchdog.heartbeat_timeout_s:
            return []

        # Already told them, and not yet time to say it again.
        if (
            tracker.missing
            and tracker.last_missing_notice is not None
            and (now - tracker.last_missing_notice).total_seconds() < watchdog.repeat_s
        ):
            return []

        tracker.missing = True
        tracker.last_missing_notice = now
        return self._emit(
            Notification(
                title=f"{tracker.node_id}: monitor silent",
                message=(f"No heartbeat for {int(silent_for)}s. Nothing is watching the light."),
                priority=watchdog.priority,
                tags=("warning",),
                kind=KIND_WATCHDOG,
                node_id=tracker.node_id,
            ),
            tracker,
            now,
        )

    def _check_staleness(self, tracker: NodeTracker, now: datetime) -> list[Notification]:
        """The tap cannot tell a disconnected harness from an idle machine.

        Both look like every input dark, forever. If a working day passes with no light
        activity at all, that is worth saying out loud -- it is the only signal we have
        that the thing might be deaf rather than merely quiet.
        """
        staleness = self._config.staleness
        if not staleness.enabled or tracker.stale_notified or tracker.last_activity is None:
            return []
        if tracker.missing:
            return []  # already shouting about a bigger problem

        quiet_for = (now - tracker.last_activity).total_seconds()
        if quiet_for < staleness.after_hours * 3600:
            return []
        start, end = staleness.between
        if not (start <= now.timetz().replace(tzinfo=None) < end):
            return []

        tracker.stale_notified = True
        hours = quiet_for / 3600
        return self._emit(
            Notification(
                title=f"{tracker.node_id}: nothing seen in {hours:.0f}h",
                message=(
                    "The node is alive but has not seen the light change. Either the "
                    "machine has not run, or the tap is no longer connected to it."
                ),
                priority=staleness.priority,
                tags=("eyes",),
                kind=KIND_STALE,
                node_id=tracker.node_id,
            ),
            tracker,
            now,
        )

    # -- helpers -------------------------------------------------------------------

    def _in_cooldown(self, tracker: NodeTracker, rule: Rule, now: datetime) -> bool:
        if rule.cooldown_s <= 0:
            return False
        last = tracker.last_notified.get(rule.state)
        return last is not None and (now - last).total_seconds() < rule.cooldown_s

    def _suppressed_by_quiet_hours(self, state: str, now: datetime) -> bool:
        quiet = self._config.quiet_hours
        if state in quiet.override_states:
            return False
        return quiet.covers(now.timetz().replace(tzinfo=None))

    def _emit(
        self,
        notification: Notification,
        tracker: NodeTracker,
        now: datetime,
        *,
        mark_state: str | None = None,
    ) -> list[Notification]:
        """Send, and log the attempt either way.

        A delivery failure is recorded rather than raised: the engine's job is to keep
        processing, and a notification that could not be sent is exactly the kind of
        thing you want to find in the history afterwards. It is also queued for another
        attempt -- see :meth:`_retry_pending`, because for a latching light this is the
        only attempt that will ever be triggered by the machine itself.

        The cooldown is claimed only on success, so a failure does not silence the next
        one. ``RetryConfig`` is what stops that turning into a tight loop.
        """
        error: str | None = None
        try:
            self._notifier.send(notification)
        except NotifyError as exc:
            error = str(exc)
            log.warning(
                "notification failed",
                extra={
                    "node_id": notification.node_id,
                    "title": notification.title,
                    "error": error,
                    "queued": self._config.retry.enabled,
                },
            )
            self._enqueue(notification, tracker, now, mark_state)

        if error is None and mark_state is not None:
            tracker.last_notified[mark_state] = now

        self._history.record_notification(
            node_id=notification.node_id or tracker.node_id,
            kind=notification.kind,
            state=notification.state,
            ts=now,
            priority=notification.priority,
            title=notification.title,
            message=notification.message,
            escalation=notification.escalation,
            delivered=error is None,
            error=error,
        )
        return [notification] if error is None else []


def seconds(**kwargs: float) -> timedelta:
    """Readability helper for tests and config maths."""
    return timedelta(**kwargs)
