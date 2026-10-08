"""SQLite history: every committed transition, and every notification sent.

The point is not audit for its own sake. With an annunciator you cannot measure machine
run time, so the numbers worth having come from counting events and the gaps between
them: how many runs completed, how often it faults, and how long a lit lamp sat waiting
for someone to deal with it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    previous    TEXT,
    since       TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    confidence  TEXT    NOT NULL,
    reasons     TEXT    NOT NULL DEFAULT '[]',
    segments    TEXT    NOT NULL DEFAULT '{}',
    seq         INTEGER
);
CREATE INDEX IF NOT EXISTS transitions_node_ts ON transitions (node_id, ts);
CREATE INDEX IF NOT EXISTS transitions_state   ON transitions (node_id, state, ts);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    state       TEXT,
    ts          TEXT    NOT NULL,
    priority    TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    escalation  INTEGER NOT NULL DEFAULT 0,
    delivered   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS notifications_node_ts ON notifications (node_id, ts);
"""


@dataclass(frozen=True)
class Transition:
    node_id: str
    state: str
    previous: str | None
    since: str
    ts: str
    confidence: str
    reasons: tuple[str, ...]
    segments: Mapping[str, Any]
    seq: int | None


class History:
    """The event log. One file, opened per process."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps a reader (a report, a dashboard) from blocking the writer, which
        # matters because this process must never stall on bookkeeping.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        log.info("history opened", extra={"path": str(self._path)})

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> History:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writing ------------------------------------------------------------------

    def record_transition(self, payload: Mapping[str, Any], previous: str | None) -> int:
        """Log one committed state change, straight from the MQTT payload."""
        cursor = self._conn.execute(
            "INSERT INTO transitions"
            " (node_id, state, previous, since, ts, confidence, reasons, segments, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.get("node_id", "?"),
                payload.get("state", "unknown"),
                previous,
                payload.get("since", ""),
                payload.get("ts", ""),
                payload.get("confidence", "ok"),
                json.dumps(list(payload.get("reasons", []))),
                json.dumps(payload.get("segments", {})),
                payload.get("seq"),
            ),
        )
        return int(cursor.lastrowid or 0)

    def record_notification(
        self,
        *,
        node_id: str,
        kind: str,
        state: str | None,
        ts: datetime,
        priority: str,
        title: str,
        message: str,
        escalation: int = 0,
        delivered: bool = True,
        error: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO notifications"
            " (node_id, kind, state, ts, priority, title, message, escalation, delivered, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                kind,
                state,
                ts.isoformat(),
                priority,
                title,
                message,
                escalation,
                int(delivered),
                error,
            ),
        )
        return int(cursor.lastrowid or 0)

    # -- reading ------------------------------------------------------------------

    def last_state(self, node_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM transitions WHERE node_id = ? ORDER BY id DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        return None if row is None else row["state"]

    def transitions(self, node_id: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if node_id is None:
            return list(
                self._conn.execute("SELECT * FROM transitions ORDER BY id DESC LIMIT ?", (limit,))
            )
        return list(
            self._conn.execute(
                "SELECT * FROM transitions WHERE node_id = ? ORDER BY id DESC LIMIT ?",
                (node_id, limit),
            )
        )

    def count_states(self, node_id: str) -> dict[str, int]:
        """How many times each state has been entered. Throughput and fault counts."""
        return {
            row["state"]: row["n"]
            for row in self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM transitions WHERE node_id = ?"
                " GROUP BY state ORDER BY n DESC",
                (node_id,),
            )
        }

    def dwell_times(self, node_id: str, state: str) -> list[float]:
        """Seconds each occurrence of ``state`` lasted before the next transition.

        On a latching annunciator this is response time: how long the lamp sat lit
        before somebody dealt with it.
        """
        rows = list(
            self._conn.execute(
                "SELECT state, since, ts FROM transitions WHERE node_id = ? ORDER BY id",
                (node_id,),
            )
        )
        out: list[float] = []
        for current, following in pairwise(rows):
            if current["state"] != state:
                continue
            try:
                started = datetime.fromisoformat(current["since"].replace("Z", "+00:00"))
                ended = datetime.fromisoformat(following["since"].replace("Z", "+00:00"))
            except ValueError:
                continue
            out.append((ended - started).total_seconds())
        return out

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        """Escape hatch for ad-hoc reporting queries."""
        cursor = self._conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
