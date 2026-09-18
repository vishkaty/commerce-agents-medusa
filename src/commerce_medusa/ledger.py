"""A durable staged-change ledger with what an idempotent apply needs.

Same interface as upstream's in-memory ``ChangeLedger`` (``stage``, ``get``, ``pending``,
``applied``, ``resolved``, ``apply``, ``discard``) so a ``MerchantBackend`` swaps it in,
plus:

- ``claim`` / ``release``: an exclusive, durable mark that one process is applying the
  change, taken with a single conditional UPDATE so two hosts cannot both win;
- ``record_progress`` / ``progress``: per-item records of what was already written to the
  platform, so a retry after a crash finishes the change without writing twice.

SQLite through the standard library: one file, transactional, survives restarts, and a
deployment that wants Postgres changes the connection, not the callers.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commerce_common.fencing import truncate_display
from merchant_agent import (
    ActorKind,
    ChangeItem,
    ChangeKind,
    ChangeStatus,
    MerchantAgentConfig,
    StagedChange,
)
from merchant_agent.changes import (
    ChangeLedger,
    ChangeNotApplicable,
    GuardrailViolation,
    check_guardrails,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL,
    body TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
    change_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (change_id, key)
);
CREATE TABLE IF NOT EXISTS schedule (
    change_id TEXT PRIMARY KEY,
    apply_at TEXT NOT NULL,
    scheduled_by TEXT NOT NULL
);
"""


class SqliteLedger(ChangeLedger):
    def __init__(self, config: MerchantAgentConfig, path: Path | str = ":memory:") -> None:
        super().__init__(config)
        self.path = Path(path) if path != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL") if isinstance(self.path, Path) else None
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    # -- storage ------------------------------------------------------------------------

    def _put(self, change: StagedChange, sequence: int | None = None) -> None:
        if sequence is None:
            row = self._db.execute(
                "SELECT sequence FROM changes WHERE change_id = ?", (change.change_id,)
            ).fetchone()
            sequence = int(row[0]) if row else 0
        self._db.execute(
            "INSERT INTO changes (change_id, sequence, status, body, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(change_id) DO UPDATE SET status=excluded.status, "
            "body=excluded.body, updated_at=excluded.updated_at",
            (
                change.change_id,
                sequence,
                change.status.value,
                change.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _rows(self, where: str = "1=1", params: tuple = ()) -> list[StagedChange]:
        rows = self._db.execute(
            f"SELECT body FROM changes WHERE {where} ORDER BY sequence", params
        ).fetchall()
        return [StagedChange.model_validate_json(body) for (body,) in rows]

    # -- the upstream interface ---------------------------------------------------------

    def stage(
        self,
        *,
        kind: ChangeKind,
        summary: str,
        items: list[ChangeItem],
        actor: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
        currency: str | None = None,
        margin_impact: float | None = None,
        margin_before_pct: float | None = None,
        margin_after_pct: float | None = None,
        guardrail_notes: list[str] | None = None,
    ) -> StagedChange:
        violations = check_guardrails(kind, items, self._config)
        if violations:
            raise GuardrailViolation(violations)
        # Conflict flag: a pending change on the same target and field is flagged, not refused;
        # the operator sees both on the queue and whichever applies later wins.
        notes = list(guardrail_notes or [])
        for other in self.pending():
            overlap = sorted(
                {(i.target, i.field) for i in items} & {(i.target, i.field) for i in other.items}
            )
            if overlap:
                fields = ", ".join(f"{target} {field}" for target, field in overlap)
                notes.append(
                    f"conflicts with pending {other.change_id} on {fields}; "
                    "whichever applies later wins"
                )
        guardrail_notes = notes
        with self._db:  # BEGIN ... COMMIT: the sequence and the row land together
            self._db.execute("BEGIN IMMEDIATE")
            (last,) = self._db.execute("SELECT COALESCE(MAX(sequence), 0) FROM changes").fetchone()
            sequence = int(last) + 1
            change = StagedChange(
                change_id=f"chg-{sequence:04d}",
                kind=kind,
                status=ChangeStatus.STAGED,
                summary=truncate_display(summary, 200),
                items=items,
                created_at=datetime.now(UTC),
                created_by=actor,
                created_by_kind=actor_kind,
                guardrail_notes=guardrail_notes or [],
                currency=currency,
                margin_impact=margin_impact,
                margin_before_pct=margin_before_pct,
                margin_after_pct=margin_after_pct,
            )
            self._put(change, sequence)
            self._db.execute("COMMIT")
        return change

    def get(self, change_id: str) -> StagedChange | None:
        rows = self._rows("change_id = ?", (change_id,))
        return rows[0] if rows else None

    def pending(self) -> list[StagedChange]:
        return self._rows("status = ?", (ChangeStatus.STAGED.value,))

    def applied(self) -> list[StagedChange]:
        return self._rows("status = ?", (ChangeStatus.APPLIED.value,))

    def resolved(self) -> list[StagedChange]:
        return self._rows("status != ?", (ChangeStatus.STAGED.value,))

    def _require_staged(self, change_id: str, verb: str) -> StagedChange:
        change = self.get(change_id)
        if change is None:
            raise ChangeNotApplicable(f"no change {change_id} to {verb}")
        if change.status is not ChangeStatus.STAGED:
            raise ChangeNotApplicable(f"{change_id} is already {change.status.value}")
        return change

    def apply(self, change_id: str, actor: str, notes: list[str] | None = None) -> StagedChange:
        change = self._require_staged(change_id, "apply")
        violations = check_guardrails(change.kind, change.items, self._config)
        if violations:
            raise GuardrailViolation(violations)
        updated = change.model_copy(
            update={
                "status": ChangeStatus.APPLIED,
                "applied_at": datetime.now(UTC),
                "applied_by": actor,
                "guardrail_notes": [*change.guardrail_notes, *(notes or [])],
            }
        )
        self._put(updated)
        self._db.execute(
            "UPDATE changes SET claimed_by = NULL, claimed_at = NULL WHERE change_id = ?",
            (change_id,),
        )
        return updated

    def discard(
        self, change_id: str, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR
    ) -> StagedChange:
        change = self._require_staged(change_id, "discard")
        updated = change.model_copy(
            update={
                "status": ChangeStatus.DISCARDED,
                "discarded_at": datetime.now(UTC),
                "discarded_by": actor,
                "discarded_by_kind": actor_kind,
            }
        )
        self._put(updated)
        return updated

    def conflicts(self, change_id: str) -> list[str]:
        """Pending changes that touch a target and field this change also touches."""
        change = self.get(change_id)
        if change is None:
            return []
        mine = {(i.target, i.field) for i in change.items}
        return [
            other.change_id
            for other in self.pending()
            if other.change_id != change_id and mine & {(i.target, i.field) for i in other.items}
        ]

    # -- scheduled apply ---------------------------------------------------------

    def schedule(self, change_id: str, *, at: datetime, by: str) -> None:
        self._require_staged(change_id, "schedule")
        self._db.execute(
            "INSERT INTO schedule (change_id, apply_at, scheduled_by) VALUES (?,?,?) "
            "ON CONFLICT(change_id) DO UPDATE SET apply_at=excluded.apply_at, "
            "scheduled_by=excluded.scheduled_by",
            (change_id, at.astimezone(UTC).isoformat(), by),
        )

    def unschedule(self, change_id: str) -> None:
        self._db.execute("DELETE FROM schedule WHERE change_id = ?", (change_id,))

    def scheduled_at(self, change_id: str) -> datetime | None:
        row = self._db.execute(
            "SELECT apply_at FROM schedule WHERE change_id = ?", (change_id,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def due(self, now: datetime) -> list[str]:
        """Staged changes whose scheduled time has passed, earliest first."""
        rows = self._db.execute(
            "SELECT s.change_id FROM schedule s JOIN changes c ON c.change_id = s.change_id "
            "WHERE s.apply_at <= ? AND c.status = ? ORDER BY s.apply_at, c.sequence",
            (now.astimezone(UTC).isoformat(), ChangeStatus.STAGED.value),
        ).fetchall()
        return [row[0] for row in rows]

    # -- what an idempotent apply needs -------------------------------------------------

    def claim(self, change_id: str, owner: str) -> bool:
        """Mark the change as being applied by ``owner``; False when it is not staged or
        another owner holds it. One conditional UPDATE, so two processes cannot both win."""
        cursor = self._db.execute(
            "UPDATE changes SET claimed_by = ?, claimed_at = ? "
            "WHERE change_id = ? AND status = ? AND claimed_by IS NULL",
            (owner, datetime.now(UTC).isoformat(), change_id, ChangeStatus.STAGED.value),
        )
        return cursor.rowcount == 1

    def release(self, change_id: str) -> None:
        self._db.execute(
            "UPDATE changes SET claimed_by = NULL, claimed_at = NULL WHERE change_id = ?",
            (change_id,),
        )

    def claimed_by(self, change_id: str) -> str | None:
        row = self._db.execute(
            "SELECT claimed_by FROM changes WHERE change_id = ?", (change_id,)
        ).fetchone()
        return row[0] if row else None

    def record_progress(self, change_id: str, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO progress (change_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(change_id, key) DO UPDATE SET value=excluded.value",
            (change_id, key, json.dumps(value)),
        )

    def progress(self, change_id: str) -> dict[str, Any]:
        rows = self._db.execute(
            "SELECT key, value FROM progress WHERE change_id = ?", (change_id,)
        ).fetchall()
        return {key: json.loads(value) for key, value in rows}
