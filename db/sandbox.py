"""Simulation sandbox: the ONLY module that runs SQL against the database.

Owner: Person 1.
STATUS: get_plan, cost, index_size_bytes (real build), timed_run, checked_run, table_stats,
        measure_write_latency = REAL.
        slow_queries, run_workload = REAL. (No stubs left.)
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
TIMING_REPEATS = 9         # timed_run() runs the query this many times (after one
                           # warm-up run) and reports the middle value. More =
                           # steadier numbers but slower; 3-10 is sensible.
WRITE_TEST_ROWS = 10000    # rows inserted per write-latency batch. More = steadier
                           # per-row average but slower; 5000-50000 is sensible.
WRITE_REPEATS = 9          # write batches timed in each round (after one
                           # warm-up each); the median is used. 3-10 is sensible.
WORKLOAD_REPEATS = 3       # run_workload() runs each known slow query this many
                           # times, so slow_queries() has real history to show.
                           # Higher = more realistic call counts, slower (q04 ~2.5 s/run).

# ============================================================
# FIXED SETTINGS - must match docker-compose.yml
# ============================================================
DATABASE = "sales"           # which database every function works on; switch with
                             # use_database("retail") for the real-data check
DSN = f"postgresql://tuner:tuner@localhost:5432/{DATABASE}"
SANDBOX_SCHEMA = "sandbox"   # copies for real timing / write tests live here
SANDBOX_LOCK_ID = 4242       # advisory lock: one sandbox experiment at a time,
                             # even when several team chats call timed_run()

# Only plain "CREATE [UNIQUE] INDEX [name] ON table [USING method] (cols...)" is
# accepted as a hypothetical index - nothing else can sneak through cost().
_INDEX_SQL = re.compile(
    r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(\w+\s+)?ON\s+(?P<table>[\w.]+)\s*(USING\s+\w+\s*)?\([^;]+\)\s*;?\s*$",
    re.IGNORECASE)

# Partitioning action, format agreed with Person 3 (masked form:
# "PARTITION t_a3f BY RANGE (c_d42) MONTHLY"). Only monthly date ranges for now.
_PARTITION_SQL = re.compile(
    r"^\s*PARTITION\s+(?P<table>\w+)\s+BY\s+RANGE\s*\(\s*(?P<column>\w+)\s*\)\s+MONTHLY\s*;?\s*$",
    re.IGNORECASE)

_conn = None
_sandbox_conn = None


def use_database(name):
    """Point every function in this module at another database in the same
    container, e.g. "retail" (the real UCI dataset). Default is "sales".
    The sandbox copies are made per database, so the two never mix."""
    global DATABASE, DSN
    _close_connections()
    DATABASE, DSN = name, f"postgresql://tuner:tuner@localhost:5432/{name}"


def _tables():
    """The real tables of the current database (schema public)."""
    return [t for (t,) in _connection().execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1")]


@atexit.register
def _close_connections():
    global _conn, _sandbox_conn
    for c in (_conn, _sandbox_conn):
        if c is not None and not c.closed:
            c.close()
    _conn = _sandbox_conn = None


def _connection():
    """One shared READ-ONLY connection, opened on first use (the RL agent calls
    cost() a lot). Read-only means Postgres itself refuses any change to any
    table on it, whatever SQL text arrives - HypoPG still works."""
    global _conn
    if _conn is None or _conn.closed:
        # prepare_threshold=None: never turn a query into a server-side prepared
        # statement. psycopg does that after 5 identical runs, which breaks
        # EXPLAIN (GENERIC_PLAN) on queries with $1, $2 placeholders.
        _conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=None)
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
        _sandbox_conn = psycopg.connect(DSN, prepare_threshold=None)   # see _connection()
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
        for t in _tables():
            if conn.execute("SELECT to_regclass(%s)", (f"{SANDBOX_SCHEMA}.{t}",)).fetchone()[0]:
                continue
            conn.execute(f"CREATE TABLE {SANDBOX_SCHEMA}.{t} (LIKE public.{t} INCLUDING ALL)")
            conn.execute(f"INSERT INTO {SANDBOX_SCHEMA}.{t} SELECT * FROM public.{t}")
            conn.execute(f"ANALYZE {SANDBOX_SCHEMA}.{t}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_partitioned_copy(table, column):
    """Build (once) a copy of `table` split into one partition per month of
    `column`, in its own schema "part_<table>_<column>". The other tables are
    views onto the sandbox copies, so any query runs there unchanged.
    Returns the schema name. Never touches public."""
    schema = f"part_{table}_{column}"
    ensure_sandbox()
    conn = _sandbox_connection()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SANDBOX_LOCK_ID,))
        if conn.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",)).fetchone()[0]:
            conn.rollback()
            return schema
        kind = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (SANDBOX_SCHEMA, table, column)).fetchone()
        if kind is None or not kind[0].startswith(("date", "timestamp")):
            raise ValueError(f"Can only partition by a date column, not {table}.{column}")
        pk = [r[0] for r in conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
            "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = %s::regclass AND i.indisprimary", (f"{SANDBOX_SCHEMA}.{table}",))]

        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.{table} (LIKE {SANDBOX_SCHEMA}.{table} INCLUDING DEFAULTS) "
                     f"PARTITION BY RANGE ({column})")
        first, last = conn.execute(
            f"SELECT date_trunc('month', min({column}))::date, max({column}) "
            f"FROM {SANDBOX_SCHEMA}.{table}").fetchone()
        months = [m for (m,) in conn.execute(
            "SELECT generate_series(%s::date, %s::date, interval '1 month')::date", (first, last))]
        for m in months:
            conn.execute(f"CREATE TABLE {schema}.{table}_{m:%Y_%m} PARTITION OF {schema}.{table} "
                         f"FOR VALUES FROM ('{m}') TO ('{m}'::date + interval '1 month')")
        conn.execute(f"CREATE TABLE {schema}.{table}_other PARTITION OF {schema}.{table} DEFAULT")
        # A partitioned table's primary key must include the partition column.
        if pk:
            key = pk + ([column] if column not in pk else [])
            conn.execute(f"ALTER TABLE {schema}.{table} ADD PRIMARY KEY ({', '.join(key)})")
        conn.execute(f"INSERT INTO {schema}.{table} SELECT * FROM {SANDBOX_SCHEMA}.{table}")
        conn.execute(f"ANALYZE {schema}.{table}")
        for other in _tables():
            if other != table:
                conn.execute(f"CREATE VIEW {schema}.{other} AS SELECT * FROM {SANDBOX_SCHEMA}.{other}")
        conn.commit()
        return schema
    except Exception:
        conn.rollback()
        raise


def _check_select(sql):
    """Allow exactly ONE read-only query (SELECT / WITH ... SELECT / UNION)."""
    statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError(f"Only a single SELECT query is allowed: {sql[:80]!r}")


def get_plan(sql):
    """EXPLAIN (FORMAT JSON) plan of sql, without running it.
    Also accepts the $1, $2 placeholders that pg_stat_statements stores
    instead of literal values (uses Postgres 16's EXPLAIN GENERIC_PLAN)."""
    _check_select(sql)
    options = "GENERIC_PLAN, FORMAT JSON" if re.search(r"\$\d+", sql) else "FORMAT JSON"
    return _connection().execute(f"EXPLAIN ({options}) " + sql).fetchone()[0][0]


def _split_actions(actions):
    """Separate a list of actions into (partition schema or None, index SQLs).
    At most one "PARTITION <table> BY RANGE (<column>) MONTHLY" is allowed;
    with a partition, index tables must be plain names (they then resolve to
    the partitioned copy)."""
    partitions = [a for a in actions if _PARTITION_SQL.match(a)]
    indexes = [a for a in actions if not _PARTITION_SQL.match(a)]
    if len(partitions) > 1:
        raise ValueError("At most one PARTITION action at a time")
    for index_sql in indexes:
        (_check_sandbox_index if partitions else _check_index_sql)(index_sql)
    if not partitions:
        return None, indexes
    m = _PARTITION_SQL.match(partitions[0])
    return ensure_partitioned_copy(m["table"], m["column"]), indexes


def cost(sql, hypo_indexes=[]):
    """Planner total cost, optionally with HypoPG hypothetical indexes and/or
    one "PARTITION orders BY RANGE (order_date) MONTHLY" action.

    The pretend indexes exist only inside this connection and are wiped
    before and after every call, so no real index is ever created and other
    users of the database never see them. A PARTITION action makes the query
    run against the partitioned sandbox copy instead (built once, ~6 s).
    """
    schema, indexes = _split_actions(hypo_indexes)
    conn = _connection()
    try:
        if schema:
            conn.execute(f"SET search_path = {schema}, public")   # public: HypoPG functions
        conn.execute("SELECT hypopg_reset()")
        for index_sql in indexes:
            conn.execute("SELECT * FROM hypopg_create_index(%s)", (index_sql,))
        return float(get_plan(sql)["Plan"]["Total Cost"])
    finally:
        conn.execute("SELECT hypopg_reset()")
        conn.execute("RESET search_path")


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
    if table not in _NEW_ROWS:
        raise ValueError(f"No test-row recipe for table {table!r} (write test supports {sorted(_NEW_ROWS)})")
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
    then roll everything back so the indexes vanish. A PARTITION action in
    index_sqls runs everything on the partitioned copy instead."""
    _check_select(sql)
    for index_sql in index_sqls:
        if not _PARTITION_SQL.match(index_sql):
            _check_sandbox_index(index_sql)
    ensure_sandbox()
    schema, indexes = _split_actions(index_sqls)
    conn = _sandbox_connection()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SANDBOX_LOCK_ID,))
        if schema:
            conn.execute(f"SET LOCAL search_path = {schema}")    # undone by the rollback
        for index_sql in indexes:
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
                "/* sandbox */ SELECT count(*), md5(coalesce(string_agg(md5(t::text), '' "
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


def _known_query_ids():
    """{Postgres query identifier: our id like "q11"} for db/queries.py.
    Postgres gives every query shape an identifier; EXPLAIN VERBOSE reveals it."""
    from db import queries
    ids = {}
    for q in queries.QUERIES:
        plan = _connection().execute("EXPLAIN (VERBOSE, FORMAT JSON) " + q["sql"]).fetchone()[0][0]
        ids[plan["Query Identifier"]] = q
    return ids


def run_workload(repeats=None, reset=False):
    """Really run every known slow query `repeats` times on the live tables,
    READ-ONLY, like a busy shop would, so slow_queries() has history to show.
    reset=True first clears ALL recorded statistics (shared by the whole team -
    use only when preparing a clean demo)."""
    from db import queries
    conn = _connection()
    if reset:
        conn.execute("SELECT pg_stat_statements_reset()")
    for _ in range(repeats or WORKLOAD_REPEATS):
        for q in queries.QUERIES:
            conn.execute(q["sql"]).fetchall()


def slow_queries(limit=10):
    """Slowest statements actually run on the live tables, from
    pg_stat_statements: [{"query_id", "sql", "calls", "mean_ms"}], slowest first.

    Only real top-level SELECTs on our tables count; our own tooling (EXPLAIN
    calls, sandbox runs tagged /* sandbox */, catalog lookups) is left out.
    Known queries get their id ("q04") and their full SQL; anything else gets
    "pg_<identifier>" and the recorded text, with $1, $2 in place of values -
    get_plan() and cost() accept that form."""
    known = _known_query_ids()
    rows = _connection().execute(r"""
        SELECT queryid, query, calls, mean_exec_time FROM pg_stat_statements
        WHERE toplevel
          AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND query !~ '^\s*/\* sandbox \*/'
          AND query !~ 'md5\(t::text\)'            -- fingerprint runs from before the tag
          AND query ~* '^\s*(select|with)\M'
          AND query ~* '\m(orders|customers|products|regions)\M'
          AND query !~* '(pg_|information_schema|hypopg)'
        ORDER BY mean_exec_time DESC LIMIT %s""", (limit,)).fetchall()
    out = []
    for queryid, text, calls, mean_ms in rows:
        q = known.get(queryid)
        out.append({"query_id": q["id"] if q else f"pg_{queryid}",
                    "sql": q["sql"] if q else text,
                    "calls": int(calls), "mean_ms": round(mean_ms, 2)})
    return out


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
