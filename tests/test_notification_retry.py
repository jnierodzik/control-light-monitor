"""Redelivery of notifications the network would not carry.

This is the failure mode the whole project exists to avoid. The machine's light is an
annunciator: the node publishes ``error`` once, on the transition, and then the lamp
just stays lit. Nothing about the machine will ever trigger a second attempt -- so a
single failed POST used to mean the alert was gone, leaving a ``delivered=0`` row in
SQLite and a silent phone next to a faulted press.

The tests here are deliberately about the ugly cases: a network that is down and comes
back, one that never comes back, and an alert that reality overtakes while it waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stacklight.errors import ConfigError
from stacklight.rules.config import parse_rules_config
from stacklight.rules.engine import KIND_STATE, KIND_WATCHDOG, RulesEngine
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
            },
            {"state": "done", "title": "Run complete", "cooldown_s": 60},
            {"state": "clear", "notify": False},
        ],
    }
    data.update(overrides)
    return data


@pytest.fixture
def rig(tmp_path):
    def build(**overrides):
        config = parse_rules_config(rules_dict(**overrides))
        notifier = RecordingNotifier()
        history = History(tmp_path / "history.sqlite")
        clock = FakeClock()
        engine = RulesEngine(config, notifier, history, now=clock)
        return engine, notifier, history, clock

    return build


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


def notifications(history: History) -> list[dict]:
    rows = history._conn.execute(
        "SELECT kind, state, title, message, delivered, error, attempt"
        " FROM notifications ORDER BY id"
    )
    return [dict(row) for row in rows]


# -- the core guarantee ------------------------------------------------------------


def test_a_failed_alert_is_queued_not_lost(rig):
    engine, notifier, _history, _clock = rig()
    notifier.failing = True

    assert engine.on_state(NODE, state_payload("error")) == []
    assert [p.notification.title for p in engine.pending] == ["Machine error"]


def test_a_queued_alert_is_delivered_when_the_network_returns(rig):
    """The whole point: the press is still faulted, so the alert is still true."""
    engine, notifier, _history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))
    assert notifier.sent == []

    notifier.failing = False
    clock.advance(seconds=31)  # past the default retry interval
    sent = engine.tick()

    assert [n.title for n in sent] == ["Machine error"]
    assert notifier.titles() == ["Machine error"]
    assert engine.pending == ()


def test_nothing_is_retried_before_the_interval_elapses(rig):
    """Otherwise a dead network means a POST on every single tick."""
    engine, notifier, _history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    notifier.failing = False
    clock.advance(seconds=5)
    assert engine.tick() == []
    assert notifier.sent == []
    assert len(engine.pending) == 1

    clock.advance(seconds=26)
    assert len(engine.tick()) == 1


def test_a_delivered_retry_says_how_late_it_is(rig):
    """An alert arriving ten minutes on cannot read as having just happened."""
    engine, notifier, _history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    notifier.failing = False
    clock.advance(minutes=4, seconds=12)
    sent = engine.tick()

    assert "delayed 4m12s" in sent[0].message
    assert "Red light is on." in sent[0].message


def test_the_original_message_is_not_mutated(rig):
    """The queued notification is reused on every attempt, so lateness must not stack."""
    engine, notifier, _history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    for _ in range(3):
        clock.advance(seconds=31)
        engine.tick()

    assert engine.pending[0].notification.message == "Red light is on."
    notifier.failing = False
    clock.advance(seconds=31)
    sent = engine.tick()
    assert sent[0].message.count("delayed") == 1


# -- the cooldown ------------------------------------------------------------------


def test_the_cooldown_starts_when_the_alert_actually_arrives(rig):
    """Not when it was first attempted -- the operator's clock starts at the buzz."""
    engine, notifier, _history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    notifier.failing = False
    clock.advance(seconds=31)
    engine.tick()
    assert len(notifier.sent) == 1

    # 290s after the retry succeeded: inside the 300s cooldown, so still quiet.
    clock.advance(seconds=290)
    engine.on_state(NODE, state_payload("clear"))
    engine.on_state(NODE, state_payload("error"))
    assert len(notifier.sent) == 1

    clock.advance(seconds=20)
    engine.on_state(NODE, state_payload("clear"))
    engine.on_state(NODE, state_payload("error"))
    assert len(notifier.sent) == 2


# -- being overtaken by reality ----------------------------------------------------


def test_a_queued_alert_is_dropped_once_the_state_moves_on(rig):
    """A fault that has since cleared is not worth waking someone for."""
    engine, notifier, history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    notifier.failing = False
    engine.on_state(NODE, state_payload("clear"))  # notify: false, so nothing sent
    clock.advance(seconds=31)

    assert engine.tick() == []
    assert notifier.sent == []
    assert engine.pending == ()
    assert "state moved on to 'clear'" in notifications(history)[-1]["error"]


def test_a_queued_alert_survives_a_state_that_returns_to_it(rig):
    """error -> clear -> error leaves the queued alert describing the present again."""
    engine, notifier, _history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))
    engine.on_state(NODE, state_payload("clear"))
    engine.on_state(NODE, state_payload("error"))

    notifier.failing = False
    clock.advance(seconds=31)
    sent = engine.tick()

    # One alert, not two: the second error collapsed into the queued one rather than
    # queueing alongside it. And it is not superseded, because the state came back.
    assert [n.state for n in sent] == ["error"]


def test_a_flapping_light_does_not_queue_an_alert_per_transition(rig):
    """The recovery burst this prevents is the reason for collapsing duplicates."""
    engine, notifier, _history, clock = rig()
    notifier.failing = True
    for _ in range(10):
        clock.advance(seconds=2)
        engine.on_state(NODE, state_payload("error"))
        engine.on_state(NODE, state_payload("clear"))

    assert len(engine.pending) == 1

    notifier.failing = False
    clock.advance(seconds=31)
    engine.on_state(NODE, state_payload("error"))  # still faulted, so still relevant
    assert len(engine.tick()) == 1


def test_collapsing_a_duplicate_does_not_extend_its_deadline(rig):
    """Otherwise a light flapping once a minute keeps an alert alive indefinitely."""
    engine, notifier, _history, clock = rig(retry={"max_age_s": 120, "interval_s": 30})
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))
    first_attempt = engine.pending[0].first_attempt

    for _ in range(5):  # keep re-queueing the same state for 150s
        clock.advance(seconds=30)
        engine.on_state(NODE, state_payload("clear"))
        engine.on_state(NODE, state_payload("error"))
        assert all(p.first_attempt == first_attempt for p in engine.pending)
        engine.tick()

    assert engine.pending == ()


def test_a_queued_watchdog_is_dropped_once_the_node_comes_back(rig):
    engine, notifier, history, clock = rig()
    engine.on_health(NODE, {"node_id": NODE})
    notifier.failing = True
    engine.on_availability(NODE, {"status": "offline"})
    assert len(engine.pending) == 1
    assert engine.pending[0].notification.kind == KIND_WATCHDOG

    notifier.failing = False
    engine.on_health(NODE, {"node_id": NODE})  # heartbeats resumed
    clock.advance(seconds=31)
    titles = [n.title for n in engine.tick()]

    assert f"{NODE}: monitor offline" not in titles
    assert engine.pending == ()
    assert "came back" in notifications(history)[-1]["error"]


# -- giving up ---------------------------------------------------------------------


def test_an_alert_is_abandoned_once_it_is_too_old(rig):
    engine, notifier, history, clock = rig(retry={"max_age_s": 600})
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    clock.advance(seconds=601)
    assert engine.tick() == []
    assert engine.pending == ()

    last = notifications(history)[-1]
    assert last["delivered"] == 0
    assert "gave up" in last["error"]


def test_age_is_measured_from_the_first_attempt_not_the_last(rig):
    engine, notifier, _history, clock = rig(retry={"max_age_s": 120, "interval_s": 30})
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    for _ in range(4):  # 4 x 31s = 124s of repeated failure
        clock.advance(seconds=31)
        engine.tick()

    assert engine.pending == ()


def test_the_queue_is_bounded(rig):
    """A long outage across many nodes must not grow the queue without limit."""
    engine, notifier, _history, clock = rig(retry={"max_pending": 3})
    notifier.failing = True
    for i in range(6):
        clock.advance(seconds=1)
        engine.on_state(f"node{i}", state_payload("error"))

    assert len(engine.pending) == 3
    # The three that survive are the most recent, so the queue describes the shop now.
    assert [p.node_id for p in engine.pending] == ["node3", "node4", "node5"]


def test_the_newest_alerts_are_the_ones_kept(rig):
    """During an outage the latest alert is the one that describes the machine now."""
    engine, notifier, _history, _clock = rig(
        retry={"max_pending": 1},
        rules=[
            {"state": "error", "title": "first", "cooldown_s": 0},
            {"state": "done", "title": "second", "cooldown_s": 0},
        ],
    )
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))
    engine.on_state(NODE, state_payload("done"))

    assert [p.notification.title for p in engine.pending] == ["second"]


# -- the history -------------------------------------------------------------------


def test_the_first_failure_and_the_final_outcome_are_both_recorded(rig):
    """But not the attempts in between: an outage should not write a row every 30s."""
    engine, notifier, history, clock = rig()
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    for _ in range(5):
        clock.advance(seconds=31)
        engine.tick()

    notifier.failing = False
    clock.advance(seconds=31)
    engine.tick()

    rows = notifications(history)
    assert len(rows) == 2
    assert rows[0]["delivered"] == 0
    assert rows[0]["attempt"] == 1
    assert rows[1]["delivered"] == 1
    assert rows[1]["attempt"] == 7  # 1 initial + 6 retries
    assert rows[1]["kind"] == KIND_STATE


def test_retry_can_be_turned_off(rig):
    """Then the old behaviour applies: a failure is recorded and dropped."""
    engine, notifier, history, clock = rig(retry={"enabled": False})
    notifier.failing = True
    engine.on_state(NODE, state_payload("error"))

    assert engine.pending == ()
    clock.advance(seconds=600)
    assert engine.tick() == []
    assert len(notifications(history)) == 1


# -- config ------------------------------------------------------------------------


def test_retry_defaults_are_sane():
    retry = parse_rules_config(rules_dict()).retry
    assert retry.enabled is True
    assert retry.interval_s == 30.0
    assert retry.max_age_s == 1800.0
    assert retry.max_pending == 32


def test_an_interval_longer_than_the_max_age_is_rejected():
    """Otherwise every queued alert ages out before it is ever retried once."""
    with pytest.raises(ConfigError, match=r"age out"):
        parse_rules_config(rules_dict(retry={"interval_s": 600, "max_age_s": 60}))


@pytest.mark.parametrize(
    "retry",
    [
        {"interval_s": 0},
        {"max_age_s": 0},
        {"max_pending": 0},
        {"enabled": "yes"},
        {"nonsense": 1},
    ],
)
def test_bad_retry_config_is_rejected(retry):
    with pytest.raises(ConfigError):
        parse_rules_config(rules_dict(retry=retry))


# -- the migration -----------------------------------------------------------------


def test_a_database_without_the_attempt_column_is_upgraded(tmp_path):
    """There is a live history file on the Pi; the schema has to grow, not be rebuilt."""
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT,
            ts TEXT NOT NULL, priority TEXT NOT NULL, title TEXT NOT NULL,
            message TEXT NOT NULL, escalation INTEGER NOT NULL DEFAULT 0,
            delivered INTEGER NOT NULL DEFAULT 0, error TEXT
        );
        INSERT INTO notifications (node_id, kind, ts, priority, title, message, delivered)
        VALUES ('machine1', 'state', '2026-10-08T10:00:00Z', 'high', 'old row', 'kept', 1);
        """
    )
    conn.commit()
    conn.close()

    history = History(path)
    try:
        rows = notifications(history)
        assert [r["title"] for r in rows] == ["old row"]
        assert rows[0]["attempt"] == 1  # backfilled by the column default
        history.record_notification(
            node_id=NODE,
            kind="state",
            state="error",
            ts=datetime(2026, 10, 8, 11, 0, tzinfo=UTC),
            priority="high",
            title="new row",
            message="written against the upgraded schema",
            attempt=3,
        )
        assert notifications(history)[-1]["attempt"] == 3
    finally:
        history.close()


def test_opening_the_same_database_twice_is_harmless(tmp_path):
    """The migration runs on every open, so it has to be idempotent."""
    path = tmp_path / "h.sqlite"
    for _ in range(3):
        History(path).close()
    history = History(path)
    try:
        assert "attempt" in {
            row["name"] for row in history._conn.execute("PRAGMA table_info(notifications)")
        }
    finally:
        history.close()


# -- against the real notifier, not the fake ---------------------------------------
#
# Every test above uses RecordingNotifier.failing, which is a choice the fake makes
# rather than anything the network does. That is precisely the shape of an earlier bug
# in this project: the GPIO reader was double-inverting polarity and every test passed,
# because they all used the fake pin reader and none exercised the real one. So these
# drive NtfyNotifier itself, and assert that what it raises is what the queue catches.


def test_an_unreachable_server_raises_notifyerror(tmp_path):
    """The queue only engages on NotifyError, so the real client must raise exactly it."""
    from stacklight.rules.notify import Notification, NotifyError, NtfyNotifier

    config = parse_rules_config(
        rules_dict(
            # 203.0.113.0/24 is TEST-NET-3: reserved for documentation, never routed.
            ntfy={"topic": "example-topic-7k2mq9xvbn", "server": "http://203.0.113.1", "timeout_s": 1}
        )
    )
    notifier = NtfyNotifier(config.ntfy)

    with pytest.raises(NotifyError, match="could not reach"):
        notifier.send(Notification(title="t", message="m", node_id=NODE))


def test_the_engine_queues_a_real_unreachable_push(tmp_path):
    """End to end on the failure path: real notifier, real socket error, real queue."""
    from stacklight.rules.notify import NtfyNotifier

    config = parse_rules_config(
        rules_dict(
            ntfy={"topic": "example-topic-7k2mq9xvbn", "server": "http://203.0.113.1", "timeout_s": 1}
        )
    )
    clock = FakeClock()
    history = History(tmp_path / "h.sqlite")
    engine = RulesEngine(config, NtfyNotifier(config.ntfy), history, now=clock)
    try:
        assert engine.on_state(NODE, state_payload("error")) == []

        assert [p.notification.title for p in engine.pending] == ["Machine error"]
        row = notifications(history)[-1]
        assert row["delivered"] == 0
        assert "could not reach" in row["error"]

        # And it keeps trying rather than quietly abandoning it.
        clock.advance(seconds=31)
        assert engine.tick() == []
        assert engine.pending[0].attempts == 2
    finally:
        history.close()
