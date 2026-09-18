"""Order actions for the shopper as durable requests the merchant resolves.

A shopper asks to cancel an order, return items, or report a problem. None of these is
a platform write the agent may perform on its own: each becomes an ``OrderActionRequest``
in this store (SQLite, standard library), visible to the shopper as its status and to the
merchant as an open order issue. The merchant's host resolves it (approve or decline) and
only then does the platform write happen, on the merchant side.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Action = Literal["cancel", "return", "problem"]
Decision = Literal["approved", "declined"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_requests (
    request_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    action TEXT NOT NULL,
    item_ids TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    note TEXT
);
"""


class OrderActionRequest(BaseModel):
    request_id: str
    user_id: str
    order_id: str
    action: Action
    item_ids: list[str] = Field(default_factory=list)
    reason: str = Field(max_length=300)
    status: Literal["requested", "approved", "declined"] = "requested"
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    note: str | None = None


class OrderRequestStore:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = path
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    @staticmethod
    def _row(row: tuple) -> OrderActionRequest:
        (rid, _seq, user, order, action, items, reason, status, created, resolved, by, note) = row
        return OrderActionRequest(
            request_id=rid,
            user_id=user,
            order_id=order,
            action=action,
            item_ids=[i for i in items.split(",") if i],
            reason=reason,
            status=status,
            created_at=datetime.fromisoformat(created),
            resolved_at=datetime.fromisoformat(resolved) if resolved else None,
            resolved_by=by,
            note=note,
        )

    def _select(self, where: str, params: tuple = ()) -> list[OrderActionRequest]:
        rows = self._db.execute(
            f"SELECT * FROM order_requests WHERE {where} ORDER BY sequence", params
        ).fetchall()
        return [self._row(r) for r in rows]

    def create(
        self, *, user_id: str, order_id: str, action: Action, item_ids: list[str], reason: str
    ) -> OrderActionRequest:
        """Record a request; an open one for the same order and action is returned as is."""
        existing = self._select(
            "user_id = ? AND order_id = ? AND action = ? AND status = 'requested'",
            (user_id, order_id, action),
        )
        if existing:
            return existing[0]
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            (last,) = self._db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM order_requests"
            ).fetchone()
            sequence = int(last) + 1
            request = OrderActionRequest(
                request_id=f"req-{sequence:04d}",
                user_id=user_id,
                order_id=order_id,
                action=action,
                item_ids=list(item_ids),
                reason=reason[:300],
                created_at=datetime.now(UTC),
            )
            self._db.execute(
                "INSERT INTO order_requests (request_id, sequence, user_id, order_id, action, "
                "item_ids, reason, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    request.request_id,
                    sequence,
                    user_id,
                    order_id,
                    action,
                    ",".join(request.item_ids),
                    request.reason,
                    request.status,
                    request.created_at.isoformat(),
                ),
            )
            self._db.execute("COMMIT")
        return request

    def get(self, request_id: str) -> OrderActionRequest | None:
        rows = self._select("request_id = ?", (request_id,))
        return rows[0] if rows else None

    def for_user(self, user_id: str) -> list[OrderActionRequest]:
        return self._select("user_id = ?", (user_id,))

    def open(self) -> list[OrderActionRequest]:
        return self._select("status = 'requested'")

    def resolve(
        self, request_id: str, *, decision: Decision, by: str, note: str | None = None
    ) -> OrderActionRequest | None:
        """Close an open request with the operator's decision; None when there is no open
        request with that id (already resolved, or unknown)."""
        cursor = self._db.execute(
            "UPDATE order_requests SET status = ?, resolved_at = ?, resolved_by = ?, note = ? "
            "WHERE request_id = ? AND status = 'requested'",
            (decision, datetime.now(UTC).isoformat(), by, note, request_id),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(request_id)


# -- waiting for stock -------------------------------------------------------------

WAITLIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS waitlist (
    entry_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class WaitlistEntry(BaseModel):
    entry_id: str
    user_id: str
    product_id: str
    status: Literal["waiting", "notified"] = "waiting"
    created_at: datetime


class WaitlistStore:
    """Who is waiting for which sold-out product; one entry per customer and product."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(WAITLIST_SCHEMA)

    @staticmethod
    def _row(row: tuple) -> WaitlistEntry:
        entry_id, _seq, user_id, product_id, status, created = row
        return WaitlistEntry(
            entry_id=entry_id,
            user_id=user_id,
            product_id=product_id,
            status=status,
            created_at=datetime.fromisoformat(created),
        )

    def subscribe(self, *, user_id: str, product_id: str) -> WaitlistEntry:
        row = self._db.execute(
            "SELECT * FROM waitlist WHERE user_id = ? AND product_id = ? AND status = 'waiting'",
            (user_id, product_id),
        ).fetchone()
        if row:
            return self._row(row)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            (last,) = self._db.execute("SELECT COALESCE(MAX(sequence), 0) FROM waitlist").fetchone()
            entry = WaitlistEntry(
                entry_id=f"wait-{int(last) + 1:04d}",
                user_id=user_id,
                product_id=product_id,
                created_at=datetime.now(UTC),
            )
            self._db.execute(
                "INSERT INTO waitlist VALUES (?,?,?,?,?,?)",
                (
                    entry.entry_id,
                    int(last) + 1,
                    user_id,
                    product_id,
                    entry.status,
                    entry.created_at.isoformat(),
                ),
            )
            self._db.execute("COMMIT")
        return entry

    def counts(self) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT product_id, count(*) FROM waitlist WHERE status = 'waiting' GROUP BY product_id"
        ).fetchall()
        return {product_id: int(n) for product_id, n in rows}

    def for_user(self, user_id: str) -> list[WaitlistEntry]:
        rows = self._db.execute(
            "SELECT * FROM waitlist WHERE user_id = ? ORDER BY sequence", (user_id,)
        ).fetchall()
        return [self._row(r) for r in rows]
