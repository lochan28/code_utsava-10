"""Tests for Step 2 of the ai/ update: the end of pipeline.analyze() (combined fixes, timing, write cost).

Run:  .venv/Scripts/python -m app.test_tail      (from the project folder)

- a pure partition is NEVER sent to measure_write_latency / index_size_bytes (it crashes there);
- a combined fix "PARTITION ...; CREATE INDEX ..." is measured on its index part only;
- when ai/ already timed the fix (measured_ms_*), the pipeline does not time it again;
- the Approve script for a combined fix: the partition step as comments, the index as a statement.
"""
import threading
import time
import uuid
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


def run_with(recs, rl_fn=None, write_result=None):
    """analyze() with a stand-in AI returning `recs` and spies on the three measuring functions."""
    seen = {"timed_run": [], "write": [], "size": []}

    def timed(sql, idx=[]):
        seen["timed_run"].append((sql, list(idx)))
        return 4.0 if idx else 10.0

    def write(index_sql):
        seen["write"].append(index_sql)
        if isinstance(write_result, Exception):
            raise write_result
        return write_result or {"before_ms": 0.01, "after_ms": 0.012, "delta_ms": 0.002}

    def size(index_sql):
        seen["size"].append(index_sql)
        return 1_000_000
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (rl, "recommend", rl_fn or (lambda *a, **k: [dict(r) for r in recs])),
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


def test_prose_in_a_recommendation_may_contain_a_number_equal_to_a_query_literal_but_nothing_real():
    """The weekly report query has the literal 3. ai/'s reason text says "... -> 3,219": not a leak.
    The same prose with an email in it IS a leak, and so is a real value in a structural field."""
    ok, _ = run_with([rec("index", INDEX_M, reason="Simulated cost 85,864 -> 3,219 (96.3% lower).")])
    assert ok["leak_check"] is True
    bad, _ = run_with([rec("index", INDEX_M, reason="Ask zz_marker_77@example.com about it.")])
    assert bad["leak_check"] is False
    bad, _ = run_with([rec("index", INDEX_M, reason="r", verified_by="small_business")])
    assert bad["leak_check"] is False                        # a real value in a structural field stays strict


def spying_recommend(recs, calls):
    """A stand-in recommend() that takes write_fn like Person 3's and calls it once, like _attach_write_latency."""
    def recommend(masked_sql, masked_plan, cost_fn, write_fn=None, **k):
        out = [dict(r) for r in recs]
        for action in ([INDEX_M] if write_fn else []):
            for attempt in (action, PART_M):
                try:
                    calls.append(("ok", write_fn(attempt)))
                except Exception as e:                       # noqa: BLE001
                    calls.append((type(e).__name__, str(e)))
        return out
    return recommend


def test_write_fn_measures_one_plain_index_with_real_names_and_ai_gets_numbers_only():
    calls = []
    r, seen = run_with([rec("index", INDEX_M)], rl_fn=spying_recommend([rec("index", INDEX_M)], calls))
    assert seen["write"][0] == INDEX_REAL                                          # the sandbox: real names
    assert calls[0] == ("ok", {"before_ms": 0.01, "after_ms": 0.012, "delta_ms": 0.002})
    assert calls[1][0] == "ValueError" and "orders" not in calls[1][1]             # a partition is refused first
    assert seen["write"].count(PART_REAL) == 0 and PART_M not in seen["write"]


def test_write_fn_failures_carry_only_the_error_type():
    calls = []
    run_with([rec("index", INDEX_M)], rl_fn=spying_recommend([rec("index", INDEX_M)], calls),
             write_result=RuntimeError("boom with Rahul Sharma in it"))
    assert calls[0] == ("RuntimeError", "RuntimeError") and "Rahul" not in str(calls)


def test_ais_measured_write_latency_is_used_and_not_measured_again():
    wl = {"source": "measured", "before_ms_per_row": 0.01, "delta_ms_per_row": 0.004, "pct": 40.0, "indexes": 1}
    r, seen = run_with([rec("index", INDEX_M, write_latency=wl)])
    assert r["write_latency_ms"] == 0.004 and seen["write"] == []
    est = {"source": "estimated", "delta_ms_per_row": 0.9, "note": "width-based write penalty, not measured"}
    r, seen = run_with([rec("index", INDEX_M, write_latency=est)])
    assert seen["write"] == [INDEX_REAL] and r["write_latency_ms"] == 0.002        # an estimate is not used as a measurement


def test_deploy_rewrites_are_aligned_with_the_recommendations_and_real():
    first = rec("index", INDEX_M, rewrite_sql=f"SELECT count(*) FROM {T}", rewrite_rule="filter join to exists")
    second = rec("index", f"CREATE INDEX ON {T} ({C_DATE})")
    r, _ = run_with([first, second])
    assert r["deploy_rewrites"] == ["SELECT count(*) FROM orders", ""]
    assert len(r["deploy_rewrites"]) == len(r["recommendations"]) == len(r["deploy_scripts"])
    assert T not in r["deploy_rewrites"][0] and r["leak_check"] is True


PLACEHOLDER_SQL = "SELECT c.name FROM customers c WHERE c.segment = $1 AND c.customer_id <= $2"     # like the slow-query log


def test_a_query_with_placeholders_can_be_costed_many_times_once_auto_prepare_is_off():
    conn = sandbox._connection()
    conn.prepare_threshold = 5                                   # psycopg's default
    unique = uuid.uuid4().hex[:8]                                # fresh text: the driver remembers prepared statements
    for variant, switch_off in ((f"-- without the stopgap {unique}", False), (f"-- with the stopgap {unique}", True)):
        if switch_off:
            pipeline._no_auto_prepare()
        failed_at = None
        for i in range(1, 13):
            try:
                sandbox.cost(f"{PLACEHOLDER_SQL} {variant}")
            except Exception as e:                               # noqa: BLE001
                failed_at = (i, type(e).__name__)
                conn.rollback()
                break
        assert (failed_at is None) == switch_off, (variant, failed_at)
        if not switch_off:
            assert failed_at == (6, "ProtocolViolation"), failed_at   # the bug itself: the 6th run
    assert conn.prepare_threshold is None


def test_analyze_switches_auto_prepare_off_before_ai_costs_anything():
    sandbox._connection().prepare_threshold = 5
    run_with([rec("index", INDEX_M)])
    assert sandbox._connection().prepare_threshold is None


def test_the_stopgap_never_stops_an_analysis():
    def broken():
        raise RuntimeError("no connection")
    with patched(sandbox, "_connection", broken):
        pipeline._no_auto_prepare()                              # must not raise


def test_analyses_run_one_at_a_time_even_when_several_sessions_ask_together():
    """Streamlit runs each browser session in its own thread and the sandbox has one shared connection:
    two analyses at once made the sandbox fail (ReadOnlySqlTransaction). They must queue instead."""
    active, peak, done = [0], [0], []
    gate = threading.Lock()

    def slow_analysis(question):
        with gate:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with gate:
            active[0] -= 1
        done.append(question)
        return question
    with patched(pipeline, "_analyze", slow_analysis):
        threads = [threading.Thread(target=pipeline.analyze, args=(f"q{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert sorted(done) == [f"q{i}" for i in range(5)] and peak[0] == 1, (done, peak)


def test_a_failed_analysis_does_not_block_the_next_one():
    def explode(question):
        raise ValueError("rejected SQL")
    with patched(pipeline, "_analyze", explode):
        try:
            pipeline.analyze("x")
            raise AssertionError("no error")
        except ValueError:
            pass
    out = []
    with patched(pipeline, "_analyze", lambda q: out.append(q) or q):
        t = threading.Thread(target=lambda: pipeline.analyze("next"))
        t.start()
        t.join(5)
    assert out == ["next"] and not t.is_alive(), "the lock was not released after an error"


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
