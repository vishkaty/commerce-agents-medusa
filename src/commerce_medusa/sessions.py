"""A session store that survives a host restart.

Upstream's ``SessionStore`` keeps state and transcripts in two dicts and documents the six
storage methods "a deployment puts over its own store". This puts them over one SQLite file
(the lab's choice for the ledger too): the state document under a version, written
with a conditional UPDATE so a racing writer is refused as ``SessionConflictError``, and the
transcript as one row per message so an append writes only what is new.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

from demo_common import SessionConflictError, SessionStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_carts (
    session_id TEXT PRIMARY KEY,
    cart_id    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    document   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL,
    position   INTEGER NOT NULL,
    message    TEXT NOT NULL,
    PRIMARY KEY (session_id, position)
);
"""


class SqliteSessionStore(SessionStore[Any]):
    def __init__(self, state_type: type[Any], path: Path) -> None:
        super().__init__(state_type)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # -- The six storage methods.

    def read_state(self, session_id: str) -> tuple[int, dict[str, Any]] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version, document FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return int(row[0]), json.loads(row[1])

    def write_state(self, session_id: str, document: dict[str, Any], version: int) -> None:
        """Store ``document`` as ``version + 1`` only if the stored version is still
        ``version`` (0 while the session is being started): one conditional statement, so
        two processes racing on a session cannot both win."""
        payload = json.dumps(document)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if version == 0:
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if exists:
                    conn.execute("ROLLBACK")
                    raise SessionConflictError(session_id)
                conn.execute(
                    "INSERT INTO sessions (session_id, user_id, version, document) "
                    "VALUES (?, ?, 1, ?)",
                    (session_id, str(document.get("user_id", "")), payload),
                )
            else:
                cursor = conn.execute(
                    "UPDATE sessions SET version = ?, document = ?, user_id = ? "
                    "WHERE session_id = ? AND version = ?",
                    (version + 1, payload, str(document.get("user_id", "")), session_id, version),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    raise SessionConflictError(session_id)
            conn.execute("COMMIT")

    def read_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT message FROM session_messages WHERE session_id = ? ORDER BY position",
                (session_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def write_messages(self, session_id: str, messages: list[dict[str, Any]], start: int) -> None:
        """Replace the transcript from ``start`` on: an append when ``start`` is the stored
        length, the whole transcript after a turn compacted it."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM session_messages WHERE session_id = ? AND position >= ?",
                (session_id, start),
            )
            conn.executemany(
                "INSERT INTO session_messages (session_id, position, message) VALUES (?, ?, ?)",
                [
                    (session_id, start + offset, json.dumps(copy.deepcopy(message)))
                    for offset, message in enumerate(messages)
                ],
            )
            conn.execute("COMMIT")

    def delete(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.execute("COMMIT")

    def session_ids_for_user(self, user_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE user_id = ? ORDER BY rowid", (user_id,)
            ).fetchall()
        return [str(row[0]) for row in rows]


class SqliteCartMap(MutableMapping[str, str]):
    """The storefront's session-to-cart map over the same file, so a restarted host finds
    the Medusa cart a session was using (found live: the session record survived, the cart
    did not). Dict-like, one row per session."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def __getitem__(self, session_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cart_id FROM session_carts WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return str(row[0])

    def __setitem__(self, session_id: str, cart_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_carts (session_id, cart_id) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET cart_id = excluded.cart_id",
                (session_id, cart_id),
            )

    def __delitem__(self, session_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM session_carts WHERE session_id = ?", (session_id,))
        if cursor.rowcount == 0:
            raise KeyError(session_id)

    def __iter__(self) -> Iterator[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT session_id FROM session_carts ORDER BY rowid").fetchall()
        return iter([str(row[0]) for row in rows])

    def __len__(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT count(*) FROM session_carts").fetchone()[0])
