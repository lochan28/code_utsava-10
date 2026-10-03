"""Tests for Step 2 of the ai/ update: the end of pipeline.analyze() (combined fixes, timing, write cost).

Run:  .venv/Scripts/python -m app.test_tail      (from the project folder)

- a pure partition is NEVER sent to measure_write_latency / index_size_bytes (it crashes there);
- a combined fix "PARTITION ...; CREATE INDEX ..." is measured on its index part only;
- when ai/ already timed the fix (measured_ms_*), the pipeline does not time it again;
- the Approve script for a combined fix: the partition step as comments, the index as a statement.
"""
from contextlib import ExitStack

import pipeline
from ai import gnn, rl
from app import approve, llm
from app.test_pipeline_hooks import _no_ollama
from app.test_privacy import fresh_audit_log, patched
from app.test_support import MAPPING, T, C_REGION, stand_in_bottleneck
from db import sandbox

C_DATE = MAPPING.columns["order_date"]
PART_M = f"PARTITION {T} BY RANGE ({C_DATE}) MONTHLY"
INDEX_M = f"CREATE INDEX ON {T} ({C_REGION})"
PART_REAL, INDEX_REAL = "PARTITION orders BY RANGE (order_date) MONTHLY", "CREATE INDEX ON orders (region_id)"


def rec(kind, action, **extra):
    return {"kind": kind, "action": action, "cost_before": 100.0, "cost_after": 20.0, "reason": "r",
            "verified_by": "hypopg_cost", "improvement_pct": 80.0, **extra}


MEASURED = {"verified_by": "timed_run", "measured_ms_before": 800.0, "measured_ms_after": 200.0,
            "measured_gain_pct": 75.0, "planner_gain_pct": 80.0, "timing_note": "median of 5 sandbox runs each, same results"}


def run_with(recs):
    """analyze() with a stand-in AI returning `recs` and spies on the three measuring functions."""
    seen = {"timed_run": [], "write": [], "size": []}

    def timed(sql, idx=[]):
        seen["timed_run"].append((sql, list(idx)))
        return 4.0 if idx else 10.0

    def write(index_sql):
        seen["write"].append(index_sql)
        return {"delta_ms": 0.002}

    def size(index_sql):
        seen["size"].append(index_sql)
        return 1_000_000
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (rl, "recommend", lambda *a, **k: [dict(r) for r in recs]),
                                 (gnn, "find_bottleneck", stand_in_bottleneck),
                                 (sandbox, "timed_run", timed), (sandbox, "measure_write_latency", write),
                                 (sandbox, "index_size_bytes", size)]:
            stack.enter_context(patched(obj, name, value))
        r = pipeline.analyze("Why is the weekly sales report slow?")
    return r, seen


def test_a_pure_partition_is_never_sent_to_the_write_latency_or_size_functions():
    r, seen = run_with([rec("partition", PART_M)])
    assert seen["write"] == [] and seen["size"] == []
    assert r["write_latency_ms"] is None and r["storage_bytes"] is None
    assert r["deploy_sql"] == PART_REAL


def test_a_combined_fix_is_measured_on_its_index_part_only():
    r, seen = run_with([rec("partition", f"{PART_M}; {INDEX_M}", combined=True, **MEASURED)])
    assert seen["write"] == [INDEX_REAL] and seen["size"] == [INDEX_REAL]          # real names, index part only
    assert all("PARTITION" not in s for s in seen["write"] + seen["size"])
    assert r["write_latency_ms"] == 0.002 and r["storage_bytes"] == 1_000_000
    assert r["deploy_sql"] == f"{PART_REAL}; {INDEX_REAL}"


def test_a_combined_fix_with_no_single_index_part_measures_nothing():
    r, seen = run_with([rec("partition", f"{PART_M}; {INDEX_M}; {INDEX_M}", combined=True)])
    assert seen["write"] == [] and seen["size"] == []


def test_when_ai_already_timed_the_fix_the_pipeline_does_not_time_it_again():
    r, seen = run_with([rec("index", INDEX_M, **MEASURED)])
    assert seen["timed_run"] == []
    assert (r["time_before_ms"], r["time_after_ms"]) == (800.0, 200.0)
    assert seen["write"] == [INDEX_REAL]                                           # write cost still measured


def test_a_plain_index_that_ai_did_not_time_is_still_timed_by_the_pipeline():
    r, seen = run_with([rec("index", INDEX_M)])
    assert [idx for _, idx in seen["timed_run"]] == [[], [INDEX_REAL]]
    assert (r["time_before_ms"], r["time_after_ms"]) == (10.0, 4.0)


def test_approve_script_for_a_combined_fix_has_the_partition_as_comments_and_the_index_as_a_statement():
    script = approve.build_script("partition", f"{PART_REAL}; {INDEX_REAL}", "measured")
    lines = script.splitlines()
    body = lines[lines.index("BEGIN;") + 1:-1]
    statements = [x for x in body if not x.startswith("--")]
    assert statements == [INDEX_REAL + ";"], statements
    assert any(PART_REAL in x and x.startswith("--") for x in body)
    pure = approve.build_script("partition", PART_REAL)
    assert all(x.startswith("--") for x in pure.splitlines()[pure.splitlines().index("BEGIN;") + 1:-1])


def run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            print(f"RUN   {name}")
            fn()
            print(f"PASS  {name}")
        except Exception as e:                       # noqa: BLE001 - report every failure
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:300]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
