"""Simulation sandbox: the ONLY module that runs SQL against the database.

Owner: Person 1.
STATUS: get_plan, cost = REAL.  Other functions are still STUBS (fake numbers).
Keep the signatures and return shapes (see CLAUDE.md "Shared interface contract").

Self-test:  .venv/Scripts/python -m db.sandbox
"""
import re

import psycopg
import sqlglot
from sqlglot import exp

# ============================================================
# TUNABLE SETTINGS
# ============================================================
STATEMENT_TIMEOUT_S = 30   # Safety net: any single SQL call slower than this is
                           # cancelled. EXPLAIN without ANALYZE never runs the
                           # query, so cost() normally takes milliseconds.

# ============================================================
# FIXED SETTINGS - must match docker-compose.yml
# ============================================================
DSN = "postgresql://tuner:tuner@localhost:5432/sales"
SANDBOX_SCHEMA = "sandbox"   # copies for real timing / write tests live here

# Only plain "CREATE [UNIQUE] INDEX [name] ON table [USING method] (cols...)" is
# accepted as a hypothetical index - nothing else can sneak through cost().
_INDEX_SQL = re.compile(
    r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(\w+\s+)?ON\s+[\w.]+\s*(USING\s+\w+\s*)?\([^;]+\)\s*;?\s*$",
    re.IGNORECASE)

_conn = None


def _connection():
    """One shared READ-ONLY connection, opened on first use (the RL agent calls
    cost() a lot). Read-only means Postgres itself refuses any change to any
    table on it, whatever SQL text arrives - HypoPG still works."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(DSN, autocommit=True)
        _conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
        _conn.execute("SET default_transaction_read_only = on")
    return _conn


def _check_index_sql(index_sql):
    if not _INDEX_SQL.match(index_sql):
        raise ValueError(f"Not a plain CREATE INDEX statement: {index_sql!r}")


def _check_select(sql):
    """Allow exactly ONE read-only query (SELECT / WITH ... SELECT / UNION)."""
    statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError(f"Only a single SELECT query is allowed: {sql[:80]!r}")


def get_plan(sql):
    """EXPLAIN (FORMAT JSON) plan of sql, without running it."""
    _check_select(sql)
    return _connection().execute("EXPLAIN (FORMAT JSON) " + sql).fetchone()[0][0]


def cost(sql, hypo_indexes=[]):
    """Planner total cost, optionally with HypoPG hypothetical indexes.

    The pretend indexes exist only inside this connection and are wiped
    before and after every call, so no real index is ever created and other
    users of the database never see them.
    """
    for index_sql in hypo_indexes:
        _check_index_sql(index_sql)
    conn = _connection()
    try:
        conn.execute("SELECT hypopg_reset()")
        for index_sql in hypo_indexes:
            conn.execute("SELECT * FROM hypopg_create_index(%s)", (index_sql,))
        return float(get_plan(sql)["Plan"]["Total Cost"])
    finally:
        conn.execute("SELECT hypopg_reset()")


def index_size_bytes(index_sql):
    """HypoPG's estimated size of the index (storage impact)."""
    return 22_000_000


def measure_write_latency(index_sql):
    """Per-insert latency on a sandbox copy, without vs with the index."""
    return {"before_ms": 0.050, "after_ms": 0.052, "delta_ms": 0.002}


def timed_run(sql, index_sqls=[]):
    """Real execution time (ms) on a sandbox copy with these indexes built."""
    return 27.0 if not index_sqls else 3.0


def slow_queries(limit=10):
    """Slowest statements from pg_stat_statements."""
    return [{"query_id": "q11", "sql": "SELECT ...", "calls": 40, "mean_ms": 27.0}][:limit]


if __name__ == "__main__":
    from db import queries
    q11 = queries.get("q11")["sql"]
    fix = "CREATE INDEX ON orders (region_id, order_date)"
    print("plan root:", get_plan(q11)["Plan"]["Node Type"])
    print("q11 cost before/after:", cost(q11), cost(q11, [fix]))
    print("index size:", index_size_bytes("CREATE INDEX ON orders (region_id)"))
    print("write latency:", measure_write_latency("CREATE INDEX ON orders (region_id)"))
    print("timed run:", timed_run("SELECT 1"), timed_run("SELECT 1", ["x"]))
    print("slow queries:", slow_queries())
