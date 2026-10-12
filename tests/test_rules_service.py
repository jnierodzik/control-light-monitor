"""The rules service: topic parsing, dispatch, and robustness against bad input.

The judgement all lives in the engine, which is tested separately. What matters here is
that messages reach it intact and that nothing a broker can deliver is able to kill the
process -- this is the thing that tells you the machine needs you.
"""

from __future__ import annotations

import json

import pytest

from stacklight.rules.config import parse_rules_config
from stacklight.rules.engine import RulesEngine
from stacklight.rules.notify import RecordingNotifier
from stacklight.rules.service import RulesService, parse_topic
from stacklight.rules.store import History

NODE = "machine1"
BASE = "shop/lights"


@pytest.fixture
def service(tmp_path):
    config = parse_rules_config(
        {
            "mqtt": {"base_topic": BASE},
            "ntfy": {"topic": "example-topic-not-a-real-one"},
            "rules": [
                {"state": "error", "title": "Machine fault", "priority": "high"},
                {"state": "done", "title": "Run complete"},
                {"state": "clear", "notify": False},
            ],
        }
    )
    notifier = RecordingNotifier()
    history = History(tmp_path / "h.sqlite")
    engine = RulesEngine(config, notifier, history)
    return RulesService(config, engine), notifier, history


def state_message(state: str) -> bytes:
    return json.dumps(
        {
            "node_id": NODE,
            "state": state,
            "segments": {"red": {"mode": "off", "value": 0.0}},
            "since": "2026-10-08T10:00:00Z",
            "ts": "2026-10-08T10:00:02Z",
            "confidence": "ok",
            "reasons": [],
        }
    ).encode()


# -- topic parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        (f"{BASE}/machine1/state", ("machine1", "state")),
        (f"{BASE}/machine1/health", ("machine1", "health")),
        (f"{BASE}/machine1/availability", ("machine1", "availability")),
        (f"{BASE}/system2/state", ("system2", "state")),
        (f"{BASE}/machine1/snapshot", None),  # not ours to act on
        (f"{BASE}/machine1", None),
        (f"{BASE}/machine1/state/extra", None),
        ("other/ns/machine1/state", None),
        ("", None),
    ],
)
def test_topic_parsing(topic, expected):
    assert parse_topic(topic, BASE) == expected


def test_a_second_node_needs_no_central_change(service):
    """Adding another machine is a deployment change, not a code change."""
    svc, notifier, history = service
    svc.handle(f"{BASE}/machine1/state", state_message("error"))
    svc.handle(f"{BASE}/system2/state", state_message("error"))
    assert len(notifier.sent) == 2
    assert {n.node_id for n in notifier.sent} == {"machine1", "system2"}
    # Each node gets its own tracker and its own history, keyed off the topic.
    assert {row["node_id"] for row in history.transitions()} == {"machine1", "system2"}


def test_history_is_keyed_on_the_topic_not_the_payload(service):
    """A node whose payload disagrees with its topic must not split its own history.

    The topic is the broker's namespace and is what the engine routes on, so it wins.
    """
    svc, _notifier, history = service
    svc.handle(f"{BASE}/system2/state", state_message("error"))  # payload says machine1
    assert [row["node_id"] for row in history.transitions()] == ["system2"]


# -- dispatch ----------------------------------------------------------------------


def test_a_state_message_reaches_the_engine(service):
    svc, notifier, _ = service
    svc.handle(f"{BASE}/machine1/state", state_message("error"))
    assert notifier.titles() == ["Machine fault"]


def test_an_availability_offline_raises_the_watchdog(service):
    svc, notifier, _ = service
    svc.handle(f"{BASE}/machine1/health", b'{"node_id": "machine1"}')
    svc.handle(f"{BASE}/machine1/availability", b'{"status": "offline"}')
    assert notifier.sent[-1].kind == "watchdog"


def test_an_irrelevant_topic_is_ignored(service):
    svc, notifier, history = service
    svc.handle(f"{BASE}/machine1/snapshot", b"\xff\xd8not-json")
    assert notifier.sent == []
    assert history.transitions() == []


# -- nothing a broker delivers may kill the process --------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b"not json at all",
        b"",
        b"[1, 2, 3]",  # valid JSON, wrong shape
        b'"a string"',
        b"null",
        b"\xff\xfe\x00binary",  # not even UTF-8
        b'{"state": ',  # truncated
    ],
)
def test_a_malformed_payload_is_dropped_not_raised(service, payload):
    svc, notifier, _ = service
    assert svc.handle(f"{BASE}/machine1/state", payload) == []
    assert notifier.sent == []


def test_a_state_payload_missing_fields_still_works(service):
    """Nodes are supposed to send a full document, but a partial one must not crash
    the thing that tells you the machine needs you."""
    svc, notifier, history = service
    svc.handle(f"{BASE}/machine1/state", b'{"node_id": "machine1", "state": "error"}')
    assert notifier.titles() == ["Machine fault"]
    assert len(history.transitions()) == 1


def test_an_exploding_engine_does_not_take_down_the_service(service, monkeypatch):
    svc, _notifier, _history = service

    def boom(*_args, **_kwargs):
        raise RuntimeError("engine fell over")

    monkeypatch.setattr(svc._engine, "on_state", boom)
    assert svc.handle(f"{BASE}/machine1/state", state_message("error")) == []


# -- the loop ----------------------------------------------------------------------


def test_run_drains_queued_messages_and_stops(service):
    svc, notifier, _ = service
    for state in ("error", "clear", "done"):
        svc._queue.put((f"{BASE}/machine1/state", state_message(state)))

    svc.run(max_seconds=0.5)

    assert notifier.titles() == ["Machine fault", "Run complete"]


def test_drain_processes_everything_pending(service):
    svc, notifier, _ = service
    svc._queue.put((f"{BASE}/machine1/state", state_message("error")))
    assert svc.drain() == 1
    assert svc.drain() == 0
    assert notifier.titles() == ["Machine fault"]


def test_stop_ends_the_loop(service):
    svc, _, _ = service
    svc.stop()
    svc.run(max_seconds=60.0)  # returns immediately rather than looping for a minute


# -- a planned restart should be quiet ---------------------------------------------


def test_a_shutting_down_service_does_not_raise_the_watchdog(service):
    """systemd restarts both units together. The node publishes `offline` as it exits,
    and this process -- still draining its queue after its own SIGTERM -- used to send
    a watchdog alert one second before stopping as well. Observed on the real unit:

        22:11:55  rules: shutting down signal='SIGTERM'
        22:11:56  rules: notification sent 'machine1: monitor offline'

    A planned restart must not page anyone.
    """
    svc, notifier, _history = service
    svc.handle(f"{BASE}/machine1/health", b'{"node_id": "machine1"}')
    notifier.clear()

    svc.stop()
    assert svc.handle(f"{BASE}/machine1/availability", b'{"status": "offline"}') == []
    assert notifier.sent == []


def test_a_node_dying_on_its_own_still_raises_the_watchdog(service):
    """The fix must not silence the case it exists for: this process keeps running, so
    the flag is never set, and the outage is reported."""
    svc, notifier, _history = service
    svc.handle(f"{BASE}/machine1/health", b'{"node_id": "machine1"}')
    notifier.clear()

    svc.handle(f"{BASE}/machine1/availability", b'{"status": "offline"}')
    assert [n.kind for n in notifier.sent] == ["watchdog"]


def test_state_messages_are_dropped_during_shutdown_too(service):
    """Not just the watchdog -- a process on its way out should not be deciding
    anything, because it cannot see what happens next."""
    svc, notifier, history = service
    svc.stop()
    assert svc.handle(f"{BASE}/machine1/state", state_message("error")) == []
    assert notifier.sent == []
    assert history.transitions() == []
