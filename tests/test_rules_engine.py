"""Rules engine behaviour, on an injected clock.

Everything here is about elapsed wall-clock time -- cooldowns, escalation after N
minutes, quiet hours, a working day with no activity. The clock is a callable the test
advances by hand, so a rule about "still lit after 15 minutes" runs instantly and
deterministically instead of being something you hope about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stacklight.errors import ConfigError
from stacklight.rules.config import parse_rules_config
from stacklight.rules.engine import KIND_STALE, KIND_WATCHDOG, RulesEngine
from stacklight.rules.notify import RecordingNotifier
from stacklight.rules.store import History

NODE = "machine1"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 8, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def rules_dict(**overrides) -> dict:
    data = {
        "mqtt": {"host": "localhost"},
        "ntfy": {"topic": "example-topic-7k2mq9xvbn"},
        "rules": [
            {
                "state": "error",
                "title": "Machine error",
                "message": "Red light is on.",
                "priority": "high",
                "cooldown_s": 300,
                "escalate_after_s": 900,
            },
            {"state": "done", "title": "Run complete", "cooldown_s": 60},
            {"state": "clear", "notify": False},
        ],
    }
    data.update(overrides)
    return data


@pytest.fixture
def rig(tmp_path):
    """A configured engine with a recording notifier and a real on-disk history."""

    def build(**overrides):
        config = parse_rules_config(rules_dict(**overrides))
        notifier = RecordingNotifier()
        history = History(tmp_path / "history.sqlite")
        clock = FakeClock()
        engine = RulesEngine(config, notifier, history, now=clock)
        return engine, notifier, history, clock

    return build


def advance_alive(engine, clock, *, hours: float) -> None:
    """Advance the clock while the node keeps heartbeating, as a live one would.

    Heartbeats deliberately do not count as activity -- a node can be perfectly healthy
    and still be seeing nothing happen, which is the whole point of the staleness rule.
    """
    remaining = hours * 3600
    while remaining > 0:
        step = min(60.0, remaining)
        clock.advance(seconds=step)
        engine.on_health(NODE, {})
        remaining -= step


def state_payload(state: str, **extra) -> dict:
    payload = {
        "node_id": NODE,
        "state": state,
        "segments": {"red": {"mode": "off", "value": 0.0}},
        "since": "2026-10-08T10:00:00Z",
        "ts": "2026-10-08T10:00:02Z",
        "confidence": "ok",
        "reasons": [],
        "seq": 1,
    }
    payload.update(extra)
    return payload


# -- the basics --------------------------------------------------------------------


def test_a_state_transition_notifies(rig):
    engine, notifier, _, _ = rig()
    engine.on_state(NODE, state_payload("error"))
    assert notifier.titles() == ["Machine error"]
    assert notifier.sent[0].priority == "high"


def test_a_state_with_notify_false_stays_quiet(rig):
    """`clear` happens after every single event. Announcing it would double the noise
    for no information."""
    engine, notifier, _, _ = rig()
    engine.on_state(NODE, state_payload("clear"))
    assert notifier.sent == []


def test_a_repeated_state_is_not_a_transition(rig):
    """Nodes republish retained state on a timer. That must not read as a new event."""
    engine, notifier, history, clock = rig()
    engine.on_state(NODE, state_payload("error"))
    for _ in range(5):
        clock.advance(minutes=1)
        engine.on_state(NODE, state_payload("error"))
    assert len(notifier.sent) == 1
    assert len(history.transitions(NODE)) == 1


def test_an_unknown_state_notifies_nothing_but_is_still_logged(rig):
    engine, notifier, history, _ = rig()
    engine.on_state(NODE, state_payload("unknown"))
    assert notifier.sent == []
    assert [r["state"] for r in history.transitions(NODE)] == ["unknown"]


# -- cooldown ----------------------------------------------------------------------


def test_cooldown_suppresses_a_rapid_repeat(rig):
    """A machine cycling error/clear/error must not produce a notification per cycle."""
    engine, notifier, _, clock = rig()
    engine.on_state(NODE, state_payload("error"))
    for _ in range(4):
        clock.advance(seconds=30)
        engine.on_state(NODE, state_payload("clear"))
        clock.advance(seconds=30)
        engine.on_state(NODE, state_payload("error"))
    assert len(notifier.sent) == 1


def test_cooldown_expires(rig):
    engine, notifier, _, clock = rig()
    engine.on_state(NODE, state_payload("error"))
    clock.advance(seconds=200)
    engine.on_state(NODE, state_payload("clear"))
    clock.advance(seconds=200)  # 400s total, past the 300s cooldown
    engine.on_state(NODE, state_payload("error"))
    assert len(notifier.sent) == 2


def test_cooldown_is_per_state(rig):
    """An error during a done cooldown still gets through."""
    engine, notifier, _, clock = rig()
    engine.on_state(NODE, state_payload("done"))
    clock.advance(seconds=5)
    engine.on_state(NODE, state_payload("error"))
    assert notifier.titles() == ["Run complete", "Machine error"]


# -- escalation --------------------------------------------------------------------


def test_a_state_that_persists_escalates(rig):
    """The light latches until someone acts, so 'still lit' is the whole point."""
    engine, notifier, _, clock = rig()
    engine.on_state(NODE, state_payload("error"))
    clock.advance(seconds=800)
    assert engine.tick() == []
    clock.advance(seconds=200)  # past 900s
    engine.tick()
    assert len(notifier.sent) == 2
    assert notifier.sent[1].priority == "urgent"
    assert notifier.sent[1].escalation == 1
    assert "16 min" in notifier.sent[1].message


def test_escalation_repeats_until_the_state_clears(rig):
    engine, notifier, _, clock = rig()
    engine.on_state(NODE, state_payload("error"))
    for _ in range(3):
        clock.advance(seconds=901)
        engine.tick()
    assert [n.escalation for n in notifier.sent] == [0, 1, 2, 3]

    engine.on_state(NODE, state_payload("clear"))
    clock.advance(seconds=5000)
    engine.tick()
    assert [n.escalation for n in notifier.sent] == [0, 1, 2, 3]  # nothing further


def test_escalation_can_be_capped(rig):
    engine, notifier, _, clock = rig(
        rules=[{"state": "error", "escalate_after_s": 600, "escalate_max": 2}]
    )
    engine.on_state(NODE, state_payload("error"))
    for _ in range(6):
        clock.advance(seconds=601)
        engine.tick()
    assert [n.escalation for n in notifier.sent] == [0, 1, 2]


def test_a_state_without_escalation_configured_never_escalates(rig):
    engine, notifier, _, clock = rig()
    engine.on_state(NODE, state_payload("done"))
    clock.advance(hours=4)
    engine.tick()
    assert len(notifier.sent) == 1


# -- quiet hours -------------------------------------------------------------------


def test_quiet_hours_suppress_routine_notifications(rig):
    engine, notifier, _, clock = rig(
        quiet_hours={"enabled": True, "start": "22:00", "end": "06:00"}
    )
    clock.now = datetime(2026, 10, 8, 23, 30, tzinfo=UTC)
    engine.on_state(NODE, state_payload("done"))
    assert notifier.sent == []


def test_quiet_hours_do_not_suppress_an_error(rig):
    """A fault at 3am is still a fault."""
    engine, notifier, _, clock = rig(
        quiet_hours={
            "enabled": True,
            "start": "22:00",
            "end": "06:00",
            "override_states": ["error"],
        }
    )
    clock.now = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
    engine.on_state(NODE, state_payload("error"))
    assert notifier.titles() == ["Machine error"]


@pytest.mark.parametrize(
    ("hour", "is_quiet"),
    [(21, False), (22, True), (23, True), (0, True), (3, True), (5, True), (6, False), (9, False)],
)
def test_the_quiet_window_crosses_midnight(hour, is_quiet):
    """22:00 to 06:00 wraps around, which is the usual shape and the easy one to get
    backwards."""
    from datetime import time

    from stacklight.rules.config import QuietHours

    window = QuietHours(enabled=True, start=time(22, 0), end=time(6, 0))
    assert window.covers(time(hour, 0)) is is_quiet


def test_a_disabled_quiet_window_never_covers_anything():
    from datetime import time

    from stacklight.rules.config import QuietHours

    assert QuietHours(enabled=False, start=time(22, 0), end=time(6, 0)).covers(time(23, 0)) is False


# -- watchdog ----------------------------------------------------------------------


def test_a_node_that_stops_heartbeating_raises_the_alarm(rig):
    """A silent monitor is worse than no monitor."""
    engine, notifier, _, clock = rig()
    engine.on_health(NODE, {})
    clock.advance(seconds=30)
    assert engine.tick() == []
    clock.advance(seconds=40)  # 70s total, past the 60s timeout
    engine.tick()
    assert notifier.sent[-1].kind == KIND_WATCHDOG
    assert "silent" in notifier.sent[-1].title


def test_the_watchdog_repeats_rather_than_telling_you_once(rig):
    engine, notifier, _, clock = rig()
    engine.on_health(NODE, {})
    clock.advance(seconds=120)
    engine.tick()
    clock.advance(seconds=600)
    engine.tick()  # inside repeat_s, nothing
    assert len(notifier.sent) == 1
    clock.advance(seconds=1400)  # past 1800s
    engine.tick()
    assert len(notifier.sent) == 2


def test_a_returning_node_says_so(rig):
    engine, notifier, _, clock = rig()
    engine.on_health(NODE, {})
    clock.advance(seconds=120)
    engine.tick()
    clock.advance(seconds=10)
    engine.on_health(NODE, {})
    assert notifier.sent[-1].title.endswith("monitor back")


def test_the_last_will_beats_the_heartbeat_timeout(rig):
    """The broker publishes availability offline the moment the socket dies, which is
    seconds rather than the full heartbeat timeout."""
    engine, notifier, _, _ = rig()
    engine.on_health(NODE, {})
    engine.on_availability(NODE, {"status": "offline"})
    assert notifier.sent[-1].kind == KIND_WATCHDOG
    assert "offline" in notifier.sent[-1].title


def test_availability_offline_does_not_double_notify(rig):
    engine, notifier, _, clock = rig()
    engine.on_health(NODE, {})
    engine.on_availability(NODE, {"status": "offline"})
    clock.advance(seconds=300)
    engine.tick()
    assert len(notifier.sent) == 1


# -- staleness ---------------------------------------------------------------------


def test_a_node_that_sees_nothing_all_day_is_flagged(rig):
    """The tap cannot distinguish an unplugged harness from an idle machine. This is
    the only signal that it might be deaf rather than quiet."""
    engine, notifier, _, clock = rig(
        staleness={"enabled": True, "after_hours": 8, "between": ["07:00", "19:00"]}
    )
    clock.now = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
    engine.on_health(NODE, {})
    advance_alive(engine, clock, hours=9)  # 17:00, inside working hours
    engine.tick()
    assert notifier.sent[-1].kind == KIND_STALE
    assert "nothing seen" in notifier.sent[-1].title


def test_staleness_only_fires_during_working_hours(rig):
    """An overnight gap is not suspicious."""
    engine, notifier, _, clock = rig(
        staleness={"enabled": True, "after_hours": 8, "between": ["07:00", "19:00"]}
    )
    clock.now = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
    engine.on_health(NODE, {})
    advance_alive(engine, clock, hours=9)  # 05:00, outside the window
    engine.tick()
    assert notifier.sent == []


def test_staleness_resets_when_the_light_does_something(rig):
    engine, notifier, _, clock = rig(
        staleness={"enabled": True, "after_hours": 8, "between": ["07:00", "19:00"]}
    )
    clock.now = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
    engine.on_health(NODE, {})
    advance_alive(engine, clock, hours=7)
    engine.on_state(NODE, state_payload("done"))
    notifier.clear()
    advance_alive(engine, clock, hours=3)  # 3h since activity, not 10
    engine.tick()
    assert notifier.sent == []


def test_staleness_stays_quiet_while_the_node_is_already_missing(rig):
    """Don't pile a second alarm on top of a bigger one."""
    engine, notifier, _, clock = rig(
        staleness={"enabled": True, "after_hours": 2, "between": ["00:00", "23:59"]}
    )
    engine.on_health(NODE, {})
    clock.advance(hours=3)
    engine.tick()
    kinds = [n.kind for n in notifier.sent]
    assert KIND_WATCHDOG in kinds
    assert KIND_STALE not in kinds


# -- history -----------------------------------------------------------------------


def test_every_transition_is_logged_with_its_predecessor(rig):
    engine, _, history, _ = rig()
    for state in ("clear", "error", "clear", "done"):
        engine.on_state(NODE, state_payload(state))
    rows = list(reversed(history.transitions(NODE)))
    assert [r["state"] for r in rows] == ["clear", "error", "clear", "done"]
    assert [r["previous"] for r in rows] == [None, "clear", "error", "clear"]


def test_notifications_are_logged_even_when_delivery_fails(rig):
    """A notification that could not be sent is exactly what you want to find in the
    history afterwards."""
    engine, notifier, history, _ = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))
    with history.cursor() as cursor:
        rows = list(cursor.execute("SELECT delivered, error FROM notifications"))
    assert len(rows) == 1
    assert rows[0]["delivered"] == 0
    assert "set to fail" in rows[0]["error"]


def test_a_failed_notification_does_not_consume_the_cooldown(rig):
    """Otherwise one network blip would silence the next five minutes of real alerts."""
    engine, notifier, _, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))
    notifier.failing = False
    clock.advance(seconds=5)
    engine.on_state(NODE, state_payload("clear"))
    engine.on_state(NODE, state_payload("error"))
    assert notifier.titles() == ["Machine error"]


def test_counts_and_dwell_times_come_out_of_the_log(rig):
    """Throughput and response time, which is what the history is for."""
    engine, _, history, clock = rig()
    for _ in range(3):
        engine.on_state(NODE, state_payload("done", since="2026-10-08T10:00:00Z"))
        clock.advance(minutes=5)
        engine.on_state(NODE, state_payload("clear", since="2026-10-08T10:04:00Z"))
        clock.advance(minutes=5)
    assert history.count_states(NODE)["done"] == 3
    dwells = history.dwell_times(NODE, "done")
    assert len(dwells) == 3
    assert all(d == pytest.approx(240.0) for d in dwells)


# -- config ------------------------------------------------------------------------


def test_a_guessable_ntfy_topic_is_rejected():
    """ntfy.sh has no accounts: the topic name is the only secret."""
    with pytest.raises(ConfigError, match="too short to be secret"):
        parse_rules_config(rules_dict(ntfy={"topic": "machine1"}))


def test_a_topic_given_as_a_url_is_rejected():
    with pytest.raises(ConfigError, match="topic name, not a URL"):
        parse_rules_config(rules_dict(ntfy={"topic": "https://ntfy.sh/machine1abcdef"}))


def test_duplicate_rules_are_rejected():
    with pytest.raises(ConfigError, match="duplicate rule"):
        parse_rules_config(
            rules_dict(rules=[{"state": "error"}, {"state": "error", "priority": "low"}])
        )


def test_an_unknown_priority_is_rejected():
    with pytest.raises(ConfigError, match="expected one of"):
        parse_rules_config(rules_dict(rules=[{"state": "error", "priority": "panic"}]))


def test_a_bad_time_is_rejected():
    with pytest.raises(ConfigError, match="24-hour time"):
        parse_rules_config(rules_dict(quiet_hours={"enabled": True, "start": "half ten"}))
