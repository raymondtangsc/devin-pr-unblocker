"""SQLite-backed state store.

Every unit of work is a ``WorkItem``: one blocked pull request, tracked from
detection through to a terminal outcome. The table is the audit trail and the
source for every metric on the dashboard, which is why state transitions are
recorded with timestamps rather than just overwritten.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Work item lifecycle. Terminal states are SUCCEEDED / FAILED / SKIPPED.
DETECTED = "detected"
ISSUE_FILED = "issue_filed"
DISPATCHED = "dispatched"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"      # the agent tried and could not resolve it -> a human must
ERRORED = "errored"    # our plumbing broke; nothing was learned about the PR
SKIPPED = "skipped"

TERMINAL = frozenset({SUCCEEDED, FAILED, ERRORED, SKIPPED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    pr_number        INTEGER PRIMARY KEY,
    repo             TEXT    NOT NULL,
    title            TEXT    NOT NULL,
    author           TEXT    NOT NULL,
    blocker          TEXT    NOT NULL,   -- dirty | unstable
    pr_age_days      REAL    NOT NULL,
    state            TEXT    NOT NULL,
    issue_number     INTEGER,
    session_id       TEXT,
    session_url      TEXT,
    acus_consumed    REAL    NOT NULL DEFAULT 0,
    detail           TEXT,
    detected_at      REAL    NOT NULL,
    dispatched_at    REAL,
    finished_at      REAL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,
    pr_number  INTEGER,
    message    TEXT NOT NULL,
    payload    TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_state ON work_items(state);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


@dataclass
class WorkItem:
    pr_number: int
    repo: str
    title: str
    author: str
    blocker: str
    pr_age_days: float
    state: str
    issue_number: int | None = None
    session_id: str | None = None
    session_url: str | None = None
    acus_consumed: float = 0.0
    detail: str | None = None
    detected_at: float = 0.0
    dispatched_at: float | None = None
    finished_at: float | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.dispatched_at and self.finished_at:
            return self.finished_at - self.dispatched_at
        return None


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI's threadpool may touch this from
        # different worker threads. All writes go through short-lived cursors.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- events

    def log(
        self, kind: str, message: str, pr_number: int | None = None, **payload: Any
    ) -> None:
        self._conn.execute(
            "INSERT INTO events (ts, kind, pr_number, message, payload) VALUES (?,?,?,?,?)",
            (
                time.time(),
                kind,
                pr_number,
                message,
                json.dumps(payload) if payload else None,
            ),
        )
        self._conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, kind, pr_number, message FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ work items

    def upsert_detected(self, item: WorkItem) -> bool:
        """Record a newly detected blocked PR.

        Returns True if this is genuinely new. Detection runs on every push to
        master, so the same PR surfaces repeatedly; without this guard each push
        would file a duplicate issue and burn a fresh Devin session.
        """
        existing = self.get(item.pr_number)
        if existing is not None:
            # Re-detection of something already in flight or finished: ignore,
            # but refresh the blocker in case it changed (unstable -> dirty).
            self._conn.execute(
                "UPDATE work_items SET blocker = ?, pr_age_days = ? WHERE pr_number = ?",
                (item.blocker, item.pr_age_days, item.pr_number),
            )
            self._conn.commit()
            return False
        self._conn.execute(
            """INSERT INTO work_items
               (pr_number, repo, title, author, blocker, pr_age_days, state, detected_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                item.pr_number,
                item.repo,
                item.title,
                item.author,
                item.blocker,
                item.pr_age_days,
                DETECTED,
                time.time(),
            ),
        )
        self._conn.commit()
        return True

    def get(self, pr_number: int) -> WorkItem | None:
        row = self._conn.execute(
            "SELECT * FROM work_items WHERE pr_number = ?", (pr_number,)
        ).fetchone()
        return WorkItem(**dict(row)) if row else None

    def all_items(self) -> list[WorkItem]:
        rows = self._conn.execute(
            "SELECT * FROM work_items ORDER BY detected_at DESC"
        ).fetchall()
        return [WorkItem(**dict(r)) for r in rows]

    def by_state(self, *states: str) -> list[WorkItem]:
        placeholders = ",".join("?" for _ in states)
        rows = self._conn.execute(
            f"SELECT * FROM work_items WHERE state IN ({placeholders})", states
        ).fetchall()
        return [WorkItem(**dict(r)) for r in rows]

    def set_issue(self, pr_number: int, issue_number: int) -> None:
        self._conn.execute(
            "UPDATE work_items SET issue_number = ?, state = ? WHERE pr_number = ?",
            (issue_number, ISSUE_FILED, pr_number),
        )
        self._conn.commit()

    def set_issue_number(self, pr_number: int, issue_number: int) -> None:
        """Re-point a work item at a different issue, leaving its state alone."""
        self._conn.execute(
            "UPDATE work_items SET issue_number = ? WHERE pr_number = ?",
            (issue_number, pr_number),
        )
        self._conn.commit()

    def set_dispatched(self, pr_number: int, session_id: str, session_url: str) -> None:
        self._conn.execute(
            """UPDATE work_items
               SET session_id = ?, session_url = ?, state = ?, dispatched_at = ?
               WHERE pr_number = ?""",
            (session_id, session_url, DISPATCHED, time.time(), pr_number),
        )
        self._conn.commit()

    def set_state(
        self,
        pr_number: int,
        state: str,
        detail: str | None = None,
        acus: float | None = None,
    ) -> None:
        finished = time.time() if state in TERMINAL else None
        self._conn.execute(
            """UPDATE work_items
               SET state = ?,
                   detail = COALESCE(?, detail),
                   acus_consumed = COALESCE(?, acus_consumed),
                   finished_at = COALESCE(?, finished_at)
               WHERE pr_number = ?""",
            (state, detail, acus, finished, pr_number),
        )
        self._conn.commit()

    # -------------------------------------------------------------- metrics

    def metrics(self) -> dict[str, Any]:
        items = self.all_items()
        by_state: dict[str, int] = {}
        for it in items:
            by_state[it.state] = by_state.get(it.state, 0) + 1

        # Success rate is about the agent's work, so system errors are excluded:
        # a dispatch that never reached Devin says nothing about whether Devin
        # could have resolved the conflict.
        finished = [i for i in items if i.state in (SUCCEEDED, FAILED)]
        succeeded = [i for i in items if i.state == SUCCEEDED]
        durations = [d for i in succeeded if (d := i.duration_seconds) is not None]

        # Success rate is measured over *attempts that reached a verdict*, so
        # queued work doesn't dilute it into meaninglessness early on.
        success_rate = (len(succeeded) / len(finished)) if finished else None

        return {
            "total_tracked": len(items),
            "by_state": by_state,
            # Queued and in-flight are different things: an issue filed with no
            # session yet is waiting for capacity, not being worked on. Counting
            # them together made "in flight" indistinguishable from the backlog.
            "queued": sum(1 for i in items if i.state == ISSUE_FILED),
            "in_flight": sum(1 for i in items if i.state in (DISPATCHED, RUNNING)),
            "succeeded": len(succeeded),
            "failed": sum(1 for i in items if i.state == FAILED),
            "errored": sum(1 for i in items if i.state == ERRORED),
            "success_rate": success_rate,
            "median_unblock_seconds": _median(durations),
            "total_acus": round(sum(i.acus_consumed for i in items), 2),
            "acus_per_success": (
                round(sum(i.acus_consumed for i in items) / len(succeeded), 2)
                if succeeded
                else None
            ),
            "blocker_mix": _count(i.blocker for i in items),
            "median_pr_age_days": _median([i.pr_age_days for i in items]),
        }


def _median(values: Iterable[float]) -> float | None:
    data = sorted(values)
    if not data:
        return None
    mid = len(data) // 2
    if len(data) % 2:
        return round(data[mid], 2)
    return round((data[mid - 1] + data[mid]) / 2, 2)


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out
