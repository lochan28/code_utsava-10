"""Simulation sandbox: the ONLY module that runs SQL against the database.

Owner: Person 1.  STATUS: STUB - returns fake numbers in the agreed shape.
Replace each function body; keep the signatures and return shapes
(see CLAUDE.md "Shared interface contract").

Self-test:  .venv/Scripts/python -m db.sandbox
"""

# ============================================================
# FIXED SETTINGS - must match docker-compose.yml
# ============================================================
DSN = "postgresql://tuner:tuner@localhost:5432/sales"
SANDBOX_SCHEMA = "sandbox"   # copies for real timing / write tests live here


def get_plan(sql):
    """EXPLAIN (FORMAT JSON) plan of sql, without running it."""
    return {"Plan": {"Node Type": "Seq Scan", "Relation Name": "orders",
                     "Total Cost": 19437.0, "Plan Rows": 1200, "Plans": []}}


def cost(sql, hypo_indexes=[]):
    """Planner total cost, optionally with HypoPG hypothetical indexes."""
    return 19437.0 if not hypo_indexes else 812.0


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
    print("plan root:", get_plan("SELECT 1")["Plan"]["Node Type"])
    print("cost before/after:", cost("SELECT 1"), cost("SELECT 1", ["CREATE INDEX ON orders (region_id)"]))
    print("index size:", index_size_bytes("CREATE INDEX ON orders (region_id)"))
    print("write latency:", measure_write_latency("CREATE INDEX ON orders (region_id)"))
    print("timed run:", timed_run("SELECT 1"), timed_run("SELECT 1", ["x"]))
    print("slow queries:", slow_queries())
