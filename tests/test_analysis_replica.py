"""The merchant's analysis delegate over a read-only view of the platform's
database, with the caps the contract names (SELECT only, rows, bytes, time, merchant
scope)."""

from __future__ import annotations

import os

import pytest
from merchant_agent import MerchantAgentConfig

from commerce_medusa.analysis import SCHEMA_NOTE, AnalysisReplica


class FakeCursor:
    def __init__(self, rows, description):
        self.rows, self.description, self.executed = rows, description, []

    async def execute(self, sql, params=None):
        self.executed.append(sql)

    async def fetchmany(self, n):
        return self.rows[:n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, rows, description):
        self.cursor_obj = FakeCursor(rows, description)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
        return False


def replica(rows, description, config=None):
    conn = FakeConnection(rows, description)

    async def connect(dsn):
        return conn

    return AnalysisReplica(
        "postgres://ro@x/db", config or MerchantAgentConfig(), connect=connect
    ), conn


async def test_select_only_and_row_cap():
    config = MerchantAgentConfig(max_analysis_rows=2)
    rep, conn = replica([(1, "a"), (2, "b"), (3, "c")], [("id",), ("name",)], config)
    table = await rep.query("SELECT id, name FROM lab_orders")
    assert table.columns == ["id", "name"] and table.row_count == 2 and table.truncated
    assert any("statement_timeout" in s.lower() for s in conn.cursor_obj.executed)
    assert conn.closed
    for bad in (
        "DELETE FROM lab_orders",
        "SELECT 1; DROP TABLE x",
        "UPDATE lab_orders SET total=0",
    ):
        with pytest.raises(ValueError):
            await rep.query(bad)


async def test_byte_cap_truncates_and_says_so():
    config = MerchantAgentConfig(max_analysis_table_chars=500)
    rep, _ = replica([(i, "x" * 200) for i in range(10)], [("id",), ("blob",)], config)
    table = await rep.query("SELECT id, blob FROM lab_orders")
    assert table.truncated and table.note and len(str(table.rows)) <= 700


def test_schema_note_names_the_views():
    assert "lab_orders" in SCHEMA_NOTE and "lab_order_lines" in SCHEMA_NOTE


@pytest.mark.medusa
@pytest.mark.skipif(not os.environ.get("DATABASE_URL_RO"), reason="no read-only DSN configured")
async def test_live_replica_counts_orders():
    rep = AnalysisReplica(os.environ["DATABASE_URL_RO"], MerchantAgentConfig())
    table = await rep.query("SELECT count(*) AS orders FROM lab_orders")
    assert table.columns == ["orders"] and table.rows[0][0] >= 1
