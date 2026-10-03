"""Simulation sandbox: the ONLY module that runs SQL against the database.

Owner: Person 1.
STATUS: get_plan, cost, index_size_bytes, timed_run = REAL.
        measure_write_latency, slow_queries are still STUBS (fake numbers).
Keep the signatures and return shapes (see CLAUDE.md "Shared interface contract").

Self-test:  .venv/Scripts/python -m db.sandbox
"""
import atexit
import re
import statistics

import psycopg
import sqlglot
from sqlglot import exp

# ============================================================
# TUNABLE SETTINGS
# ============================================================
STATEMENT_TIMEOUT_S = 30   # Safety net: any single SQL call slower than this is
                           # cancelled. EXPLAIN without ANALYZE never runs the
                           # query, so cost() normally takes milliseconds.
TIMING_REPEATS = 5         # timed_run() runs the query this many times (after one
                           # warm-up run) and reports the middle value. More =
                           # steadier numbers but slower; 3-10 is sensible.

# ============================================================
# FIXED SETTINGS - must match docker-compose.yml
# ============================================================
DSN = "postgresql://tuner:tuner@localhost:5432/sales"
SANDBOX_SCHEMA = "sandbox"   # copies for real timing / write tests live here
SANDBOX_TABLES = ["regions", "customers", "products", "orders"]
SANDBOX_LOCK_ID = 4242       # advisory lock: one sandbox experiment at a time,
                             # even when several team chats call timed_run()

# Only plain "CREATE [UNIQUE] INDEX [name] ON table [USING method] (cols...)" is
# accepted as a hypothetical index - nothing else can sneak through cost().
_INDEX_SQL = re.compile(
    r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(\w+\s+)?ON\s+(?P<table>[\w.]+)\s*(USING\s+\w+\s*)?\([^;]+\)\s*;?\s*$",
    re.IGNORECASE)

_conn = None
_sandbox_conn = None


@atexit.register
def _close_connections():
    for c in (_conn, _sandbox_conn):
        if c is not None and not c.closed:
            c.close()


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


def _check_sandbox_index(index_sql):
    """Like _check_index_sql, but the table must be unqualified so that it can
    only resolve to the sandbox copy (search_path is the sandbox schema only)."""
    _check_index_sql(index_sql)
    if "." in _INDEX_SQL.match(index_sql)["table"]:
        raise ValueError(f"Use a plain table name, no schema: {index_sql!r}")


def _sandbox_connection():
    """A WRITABLE connection that can only see the sandbox copies by name.
    Used for real index builds; every experiment is rolled back."""
    global _sandbox_conn
    if _sandbox_conn is None or _sandbox_conn.closed:
        _sandbox_conn = psycopg.connect(DSN)
        _sandbox_conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
        _sandbox_conn.execute(f"SET search_path = {SANDBOX_SCHEMA}")
        _sandbox_conn.commit()
    return _sandbox_conn


def ensure_sandbox():
    """Copy any missing table from public into the sandbox schema (with the
    same primary keys). Cheap when the copies already exist. db/generate.py
    drops the sandbox schema, so copies are rebuilt after every regenerate."""
    conn = _sandbox_connection()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SANDBOX_LOCK_ID,))
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SANDBOX_SCHEMA}")
        for t in SANDBOX_TABLES:
            if conn.execute("SELECT to_regclass(%s)", (f"{SANDBOX_SCHEMA}.{t}",)).fetchone()[0]:
                continue
            conn.execute(f"CREATE TABLE {SANDBOX_SCHEMA}.{t} (LIKE public.{t} INCLUDING ALL)")
            conn.execute(f"INSERT INTO {SANDBOX_SCHEMA}.{t} SELECT * FROM public.{t}")
            conn.execute(f"ANALYZE {SANDBOX_SCHEMA}.{t}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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
    """HypoPG's estimated size of the index on disk (storage impact), in bytes.
    Like cost(), the index is only pretend and is wiped straight after."""
    _check_index_sql(index_sql)
    conn = _connection()
    try:
        conn.execute("SELECT hypopg_reset()")
        oid = conn.execute("SELECT indexrelid FROM hypopg_create_index(%s)", (index_sql,)).fetchone()[0]
        return int(conn.execute("SELECT hypopg_relation_size(%s)", (oid,)).fetchone()[0])
    finally:
        conn.execute("SELECT hypopg_reset()")


def measure_write_latency(index_sql):
    """Per-insert latency on a sandbox copy, without vs with the index."""
    return {"before_ms": 0.050, "after_ms": 0.052, "delta_ms": 0.002}


def timed_run(sql, index_sqls=[]):
    """Real execution time (ms) of sql on the sandbox copies, with these
    indexes REALLY built there. Median of TIMING_REPEATS runs.

    Everything happens inside one transaction that is rolled back at the end,
    so the indexes vanish afterwards (even if Python crashes) and nobody else
    ever sees them. The public tables are never touched.
    """
    _check_select(sql)
    for index_sql in index_sqls:
        _check_sandbox_index(index_sql)
    ensure_sandbox()
    conn = _sandbox_connection()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SANDBOX_LOCK_ID,))
        for index_sql in index_sqls:
            conn.execute(index_sql)
        conn.execute("SET transaction_read_only = on")   # the timed query can't change anything
        times = []
        for _ in range(TIMING_REPEATS + 1):
            plan = conn.execute("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql).fetchone()[0][0]
            times.append(plan["Execution Time"])
        return round(statistics.median(times[1:]), 2)    # times[0] is the warm-up
    finally:
        conn.rollback()


def slow_queries(limit=10):
    """Slowest statements from pg_stat_statements."""
    return [{"query_id": "q11", "sql": "SELECT ...", "calls": 40, "mean_ms": 27.0}][:limit]


if __name__ == "__main__":
    from db import queries
    q11 = queries.get("q11")["sql"]
    fix = "CREATE INDEX ON orders (region_id, order_date)"
    print("plan root:", get_plan(q11)["Plan"]["Node Type"])
    print("q11 cost before/after:", cost(q11), cost(q11, [fix]))
    print("index size (MB):", index_size_bytes(fix) / 1e6)
    print("write latency:", measure_write_latency("CREATE INDEX ON orders (region_id)"))
    print("q11 real ms before/after:", timed_run(q11), timed_run(q11, [fix]))
    print("slow queries:", slow_queries())
