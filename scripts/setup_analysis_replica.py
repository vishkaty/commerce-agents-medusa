"""Create the analysis role and views on the store's Postgres and print the DSN to
keep as DATABASE_URL_RO in .env. Idempotent.

    .venv/bin/python scripts/setup_analysis_replica.py
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from commerce_medusa.analysis import VIEWS_SQL  # noqa: E402

ROLE = "lab_analysis"


def main() -> int:
    env = {
        line.split("=", 1)[0]: line.split("=", 1)[1].strip()
        for line in Path(".env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    dsn = env["DATABASE_URL"]
    password = env.get("ANALYSIS_DB_PASSWORD") or secrets.token_urlsafe(18)
    with psycopg.connect(dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL("{verb} ROLE {role} WITH LOGIN PASSWORD {password}").format(
                verb=sql.SQL(verb), role=sql.Identifier(ROLE), password=sql.Literal(password)
            )
        )
        conn.execute(VIEWS_SQL)
        conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
        conn.execute(f"GRANT SELECT ON lab_orders, lab_order_lines TO {ROLE}")
        conn.execute(f"ALTER ROLE {ROLE} SET statement_timeout = '10s'")
        conn.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on")
    parts = urlsplit(dsn)
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    ro = urlunsplit((parts.scheme, f"{ROLE}:{password}@{host}{port}", parts.path, "", ""))
    lines = Path(".env").read_text().splitlines()
    lines = [
        line for line in lines if not line.startswith(("DATABASE_URL_RO=", "ANALYSIS_DB_PASSWORD="))
    ]
    lines += [f"ANALYSIS_DB_PASSWORD={password}", f"DATABASE_URL_RO={ro}"]
    Path(".env").write_text("\n".join(lines) + "\n")
    with psycopg.connect(ro) as conn:
        count = conn.execute("SELECT count(*) FROM lab_orders").fetchone()[0]
        try:
            conn.execute("INSERT INTO lab_orders (id) VALUES ('x')")
            print("WARNING: the role could write")
        except psycopg.Error:
            pass
    print(f"role {ROLE} ready; lab_orders holds {count} orders; DATABASE_URL_RO written to .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
