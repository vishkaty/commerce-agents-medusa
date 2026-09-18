"""The merchant's analysis delegate over a read-only view of the platform database.

The contract (``MerchantBackend.execute_analysis_query``) leaves enforcement to the
implementation: a read-only role, SELECT-only statements, row and byte caps, a timeout,
and every table scoped to the merchant. Here the role is Postgres' own (created by
``scripts/setup_analysis_replica.py`` with SELECT on two views and nothing else), the
statement check is upstream's ``check_analysis_sql`` plus a keyword guard, the caps come
from ``MerchantAgentConfig``, and the views themselves are the merchant scope: this store
fronts one merchant, so the views hold only its orders.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from merchant_agent import AnalysisTable, MerchantAgentConfig
from merchant_agent.analysis import check_analysis_sql

VIEWS_SQL = """
CREATE OR REPLACE VIEW lab_orders AS
SELECT o.id, o.display_id, o.created_at, o.email, o.currency_code, o.status,
       s.totals->>'total' AS total
FROM "order" o
LEFT JOIN order_summary s ON s.order_id = o.id AND s.deleted_at IS NULL
WHERE o.deleted_at IS NULL;

CREATE OR REPLACE VIEW lab_order_lines AS
SELECT oi.order_id, li.product_id, li.variant_id, li.title, li.variant_sku,
       oi.quantity, li.unit_price
FROM order_item oi
JOIN order_line_item li ON li.id = oi.item_id
WHERE oi.deleted_at IS NULL AND li.deleted_at IS NULL;
"""

SCHEMA_NOTE = (
    "Two read-only views, this store's orders only. lab_orders(id, display_id, created_at, "
    "email, currency_code, status, total); lab_order_lines(order_id, product_id, "
    "variant_id, title, variant_sku, quantity, unit_price). Postgres dialect; one SELECT."
)

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|call|do|"
    r"pg_sleep|pg_read_file|lo_import|lo_export)\b",
    re.I,
)

Connect = Callable[[str], Awaitable[Any]]


async def _psycopg_connect(dsn: str) -> Any:
    import psycopg

    return await psycopg.AsyncConnection.connect(dsn, autocommit=True)


class AnalysisReplica:
    def __init__(
        self, dsn: str, config: MerchantAgentConfig, connect: Connect | None = None
    ) -> None:
        self._dsn, self._config = dsn, config
        self._connect = connect or _psycopg_connect

    def check(self, sql: str) -> None:
        reason = check_analysis_sql(sql)
        if reason:
            raise ValueError(reason)
        if _FORBIDDEN.search(sql):
            raise ValueError("only a read-only SELECT over the analysis views is allowed")

    async def query(self, sql: str) -> AnalysisTable:
        """Run one SELECT under the role's read-only transaction with a statement timeout;
        the result is capped by rows and by characters and says when it was cut."""
        self.check(sql)
        max_rows = self._config.max_analysis_rows
        timeout_ms = int(self._config.analysis_query_timeout_s * 1000)
        connection = await self._connect(self._dsn)
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(f"SET statement_timeout = {timeout_ms}")
                await cursor.execute("SET default_transaction_read_only = on")
                await cursor.execute(sql.strip().rstrip(";"))
                columns = [str(d[0]) for d in cursor.description or []]
                fetched = await cursor.fetchmany(max_rows + 1)
        finally:
            await connection.close()
        rows = [list(row) for row in fetched[:max_rows]]
        truncated = len(fetched) > max_rows
        note = "row-capped" if truncated else None
        cap = self._config.max_analysis_table_chars
        while rows and len(json.dumps(rows, default=str)) > cap:
            rows.pop()
            truncated, note = True, "cut to fit the size cap"
        return AnalysisTable(
            columns=columns,
            rows=[[_plain(v) for v in row] for row in rows],
            row_count=len(rows),
            truncated=truncated,
            note=note,
        )


def _plain(value: Any) -> Any:
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)
