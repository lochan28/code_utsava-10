"""Simulation sandbox: the ONLY module that runs SQL against the database.

Owner: Person 1.
STATUS: get_plan, cost, index_size_bytes (real build), timed_run, checked_run, table_stats,
        measure_write_latency = REAL.
        slow_queries is still a STUB (fake numbers).
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
WRITE_TEST_ROWS = 10000    # rows inserted per write-latency batch. More = steadier
                           # per-row average but slower; 5000-50000 is sensible.
WRITE_REPEATS = 9          # write batches timed in each round (after one
                           # warm-up each); the median is used. 3-10 is sensible.

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
    """REAL size of the index on disk (storage impact), in bytes.

    Builds the index on the sandbox copy, reads its size, then rolls back so
    it vanishes. Takes ~0.5 s on 1M orders - fine for a final recommendation,
    too slow for the RL search loop (use hypopg_index_size_bytes there).
    """
    _check_sandbox_index(index_sql)
    table = _INDEX_SQL.match(index_sql)["table"]
    ensure_sandbox()
    conn = _sandbox_connection()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SANDBOX_LOCK_ID,))
        list_indexes = "SELECT indexrelid FROM pg_index WHERE indrelid = %s::regclass"
        before = {r[0] for r in conn.execute(list_indexes, (table,))}
        conn.execute(index_sql)
        (new_oid,) = {r[0] for r in conn.execute(list_indexes, (table,))} - before
        return int(conn.execute("SELECT pg_relation_size(%s)", (new_oid,)).fetchone()[0])
    finally:
        conn.rollback()


def hypopg_index_size_bytes(index_sql):
    """HypoPG's instant GUESS of the index size, in bytes (no real build).
    Measured 3-4x too big on our data: HypoPG ignores Postgres's btree
    deduplication of repeated values. Kept for speed and for the
    "simulated vs real" accuracy comparison."""
    _check_index_sql(index_sql)
    conn = _connection()
    try:
        conn.execute("SELECT hypopg_reset()")
        oid = conn.execute("SELECT indexrelid FROM hypopg_create_index(%s)", (index_sql,)).fetchone()[0]
        return int(conn.execute("SELECT hypopg_relation_size(%s)", (oid,)).fetchone()[0])
    finally:
        conn.execute("SELECT hypopg_reset()")


# Random but realistic new rows for each table ("g" = 1..N from generate_series).
# Ids start far above the real ones so they never clash with existing keys.
_NEW_ROWS = {
    "orders": "SELECT 100000000 + g, 1 + (random()*49999)::int, 1 + (random()*9)::int, "
              "1 + (random()*1999)::int, DATE '2023-10-01' + (random()*1095)::int, "
              "1 + (random()*4)::int, round((random()*5000)::numeric, 2), "
              "(ARRAY['delivered','shipped','pending','cancelled','returned'])[1 + (random()*4)::int]",
    "customers": "SELECT 100000000 + g, 'New Customer', 'new' || g || '@example.com', "
                 "1 + (random()*9)::int, 'consumer', DATE '2026-09-30'",
    "products": "SELECT 100000000 + g, 'New item', 'Toys', round((random()*900)::numeric, 2)",
    "regions": "SELECT 100000000 + g, 'New region', 'India'",
}


def measure_write_latency(index_sql):
    """Per-insert cost (ms per row) on the sandbox copy, without vs with the index.

    Each batch inserts the same WRITE_TEST_ROWS random rows in one statement,
    timed by Postgres itself (EXPLAIN ANALYZE), so network/Python delays don't
    drown the tiny index-maintenance cost. Every batch is undone with a
    savepoint, and the whole test (index included) is rolled back at the end.

    Order is A-B-A: without index, with index, without again. Undone rows
    still take space until the end, so later batches get slightly slower;
    averaging the two "without" rounds cancels that drift.
    """
    _check_sandbox_index(index_sql)
    table = _INDEX_SQL.match(index_sql)["table"]
    insert = (f"EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) "
              f"INSERT INTO {table} SELECT * FROM new_rows")
    ensure_sandbox()
    conn = _sandbox_connection()

    def per_row_ms():
        times = []
        for _ in range(WRITE_REPEATS + 1):
            conn.execute("SAVEPOINT batch")
            times.append(conn.execute(insert).fetchone()[0][0]["Execution Time"])
            conn.execute("ROLLBACK TO SAVEPOINT batch")
        return statistics.median(times[1:]) / WRITE_TEST_ROWS   # times[0] is the warm-up

    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SANDBOX_LOCK_ID,))
        # Random rows made ONCE, so every batch inserts exactly the same rows.
        conn.execute(f"CREATE TEMP TABLE new_rows (LIKE {table}) ON COMMIT DROP")
        conn.execute(f"INSERT INTO new_rows {_NEW_ROWS[table]} "
                     f"FROM generate_series(1, {WRITE_TEST_ROWS}) g")
        list_indexes = "SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = %s::regclass"
        existing = {r[0] for r in conn.execute(list_indexes, (table,))}

        before_1 = per_row_ms()                       # A: without
        conn.execute(index_sql)
        (new_index,) = {r[0] for r in conn.execute(list_indexes, (table,))} - existing
        after = per_row_ms()                          # B: with
        conn.execute(f"DROP INDEX {new_index}")
        before_2 = per_row_ms()                       # A: without, again
        before = (before_1 + before_2) / 2
    finally:
        conn.rollback()
    return {"before_ms": round(before, 5), "after_ms": round(after, 5),
            "delta_ms": round(after - before, 5)}


def _sandbox_run(sql, index_sqls, with_checksum):
    """Shared by timed_run() and checked_run(): build the indexes for real on
    the sandbox copies, lock the transaction read-only, time sql (median of
    TIMING_REPEATS after a warm-up), optionally fingerprint its result rows,
    then roll everything back so the indexes vanish."""
    _check_select(sql)
    for index_sql in index_sqls:
        _check_sandbox_index(index_sql)
    ensure_sandbox()
    conn = _sandbox_connection()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SANDBOX_LOCK_ID,))
        for index_sql in index_sqls:
            conn.execute(index_sql)
        conn.execute("SET transaction_read_only = on")   # the query can't change anything
        times = []
        for _ in range(TIMING_REPEATS + 1):
            plan = conn.execute("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql).fetchone()[0][0]
            times.append(plan["Execution Time"])
        result = {"ms": round(statistics.median(times[1:]), 2)}   # times[0] is the warm-up
        if with_checksum:
            # Order-independent fingerprint of ALL result rows (agreed with Person 3,
            # see ai/verify.py): hash each row, sort the hashes, hash them together.
            rows, checksum = conn.execute(
                "SELECT count(*), md5(coalesce(string_agg(md5(t::text), '' "
                f"ORDER BY md5(t::text)), '')) FROM ({sql}) AS t").fetchone()
            result.update(rows=int(rows), checksum=checksum)
        return result
    finally:
        conn.rollback()


def timed_run(sql, index_sqls=[]):
    """Real execution time (ms) of sql on the sandbox copies, with these
    indexes REALLY built there. Median of TIMING_REPEATS runs.

    Everything happens inside one transaction that is rolled back at the end,
    so the indexes vanish afterwards (even if Python crashes) and nobody else
    ever sees them. The public tables are never touched.
    """
    return _sandbox_run(sql, index_sqls, with_checksum=False)["ms"]


def checked_run(sql, index_sqls=[]):
    """Like timed_run(), but also proves WHAT the query returned:
    {"ms": median time, "rows": row count, "checksum": fingerprint of all rows}.

    Two queries with the same rows + checksum returned the same result set
    (row order ignored), so a rewrite can be shown to be correct AND faster.
    Caveats: values must print the same way (e.g. 5 vs 5.00 differ), and a
    LIMIT with ties can legitimately return different rows each run.
    """
    return _sandbox_run(sql, index_sqls, with_checksum=True)


def _range_filters(sql):
    """{(table, column): ["order_date >= '2026-09-01'", ...]} for every range
    filter (>, >=, <, <=, BETWEEN) comparing a column with a constant."""
    tree = sqlglot.parse_one(sql, read="postgres")
    alias_to_table = {}
    for t in tree.find_all(exp.Table):
        alias_to_table[t.name] = t.name
        alias_to_table[t.alias_or_name] = t.name
    # Which table owns each column, for unqualified names like "order_date".
    owners = {}
    for table, column in _connection().execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (sorted(set(alias_to_table.values())),)):
        owners.setdefault(column, table)

    found = {}
    for node in tree.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
        col = node.this
        others = [node.args.get(k) for k in ("expression", "low", "high") if node.args.get(k)]
        if isinstance(node.args.get("expression"), exp.Column) and not isinstance(col, exp.Column):
            col, others = node.expression, [node.this]          # constant on the left: 5 < x
        if not isinstance(col, exp.Column) or any(o.find(exp.Column) for o in others):
            continue                                            # not "column vs constant"
        table = alias_to_table.get(col.table) if col.table else owners.get(col.name)
        if table is None:
            continue
        pred = node.copy()
        pred.find(exp.Column).replace(exp.column(col.name))     # drop the alias: o.x -> x
        found.setdefault((table, col.name), []).append(pred.sql(dialect="postgres"))
    return found


def table_stats(sql=None):
    """Table facts for Person 3's recommend(stats=...), with REAL names (the
    pipeline masks them before they reach ai/). Read from Postgres's own
    statistics - no table data is read. Format (see ai/README.md):
      rows:             {table: row count}
      eq_sel:           {"table.column": share of rows matching ONE value (1/n_distinct)}
      range_sel:        {"table.column": share of rows the planner expects for this
                         query's range filter on that column} - only when sql is given
      existing_indexes: [(table, (col, ...)), ...] incl. primary keys
    """
    conn = _connection()
    rows = {t: float(n) for t, n in conn.execute(
        "SELECT relname, reltuples FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r'")}
    eq_sel = {}
    for table, column, n_distinct in conn.execute(
            "SELECT tablename, attname, n_distinct FROM pg_stats WHERE schemaname = 'public'"):
        # n_distinct < 0 means "this fraction of the rows", e.g. -1 = every value unique
        distinct = -n_distinct * rows[table] if n_distinct < 0 else n_distinct
        eq_sel[f"{table}.{column}"] = 1.0 / max(distinct, 1.0)
    existing = [(t, tuple(cols)) for t, cols in conn.execute(
        "SELECT c.relname, array_agg(a.attname ORDER BY k.ord) "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
        "CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        "GROUP BY i.indexrelid, c.relname ORDER BY c.relname")]
    range_sel = {}
    if sql is not None:
        _check_select(sql)
        for (table, column), preds in _range_filters(sql).items():
            probe = f"SELECT 1 FROM {table} WHERE " + " AND ".join(preds)
            expected = get_plan(probe)["Plan"]["Plan Rows"]
            range_sel[f"{table}.{column}"] = min(1.0, expected / max(rows[table], 1.0))
    return {"rows": rows, "eq_sel": eq_sel, "range_sel": range_sel, "existing_indexes": existing}


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
