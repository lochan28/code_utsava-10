"""Made-up analysis result, used ONLY to look at the dashboard layout.

It has one recommendation of every kind so every badge colour is visible, and they are
deliberately in the wrong order so the page's sorting is visible too. All names are
already masked, like the real thing. The dashboard shows a big "SAMPLE DATA" warning
whenever this is on screen, so nobody can mistake it for a real analysis.

Self-test:  .venv/Scripts/python -m app.sample_data
"""

# The same plan shape that EXPLAIN (FORMAT JSON) gives, masked. Step 1 is the bottleneck.
SAMPLE_PLAN = {"Plan": {"Node Type": "Hash Join", "Total Cost": 20310.0, "Plan Rows": 9400, "Plans": [
    {"Node Type": "Seq Scan", "Relation Name": "t_a3f", "Total Cost": 18700.0, "Plan Rows": 1200000},
    {"Node Type": "Hash", "Total Cost": 920.0, "Plan Rows": 50000, "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "t_b22", "Total Cost": 920.0, "Plan Rows": 50000}]}]}}

SAMPLE_BOTTLENECK = {"node_id": 1, "operator": "Seq Scan", "op": "Seq Scan", "relation": "t_a3f",
                     "rows": 1200000, "cost_share_pct": 92.1, "path": "rules",
                     "reason": "A full scan of t_a3f reads 1,200,000 rows and is 92% of the plan's cost.",
                     "evidence": {"table": "t_a3f", "cost_share": 0.921}}

# One of each kind, in scrambled order: unsafe, rule-only, bare, simulated, measured.
SAMPLE_RECOMMENDATIONS = [
    {   # UNSAFE: faster, but returns different rows. Must never look like the best fix.
        "kind": "rewrite", "verified_by": "timed_run", "same_results": False, "unsafe_rewrite": True,
        "action": "SELECT c_1, COUNT(*) FROM t_a3f WHERE c_5 > $1 GROUP BY c_1",
        "cost_before": 18700.0, "cost_after": 3100.0, "time_before_ms": 418.0, "time_after_ms": 90.0,
        "speedup": 4.6, "reason": "Rewrote the filter, but it drops rows the original returns.",
        "verify_note": "different results: 108 rows before vs 107 after - rejected",
        "warning": "UNSAFE REWRITE: returns different rows than the original. Do not deploy."},
    {   # RULE-ONLY: a partition suggestion nobody simulated. No numbers, so no evidence.
        "kind": "partition", "verified_by": "rule_only", "novelty": "unseen shape (lower confidence)",
        "action": "PARTITION t_a3f BY RANGE (c_d42) MONTHLY",
        "reason": "The query reads one month of a table that holds three years."},
    {   # BARE: almost every key missing. Must show as unverified, never crash.
        "kind": "index", "action": "CREATE INDEX ON t_a3f (c_77)"},
    {   # SIMULATED: HypoPG planner cost only.
        "kind": "index", "verified_by": "hypopg_cost", "novelty": "unseen shape (lower confidence)",
        "action": "CREATE INDEX ON t_a3f (c_91b, c_d42)",
        "cost_before": 19437.0, "cost_after": 812.0, "improvement_pct": 95.8, "write_penalty": 0.06,
        "reason": "The filter columns c_91b and c_d42 have no index, so every row is read."},
    {   # MEASURED: really run on the sandbox copy, same rows checked.
        "kind": "rewrite", "verified_by": "timed_run", "same_results": True, "novelty": "seen query shape",
        "action": "SELECT t_b22.c_1, SUM(t_a3f.c_9) FROM t_a3f JOIN t_b22 ON t_a3f.c_5 = t_b22.c_1 "
                  "GROUP BY t_b22.c_1",
        "cost_before": 18700.0, "cost_after": 4100.0, "improvement_pct": 71.8,
        "time_before_ms": 418.0, "time_after_ms": 118.0, "speedup": 3.54,
        "verify_note": "same 108 rows, 418 ms -> 118 ms (3.5x faster)",
        "reason": "Replaced a per-row subquery with a join. Same rows, 3.5x faster.",
        "summary": "Best bundle: the rewrite (measured) plus an index on (c_91b, c_d42) (simulated).",
        "why_not": "Partitioning t_a3f by month was not simulated, so it was not preferred."},
]


# The sample comes in several variants so every state of the before/after view can be seen.
VARIANTS = ["Rewrite on top (measured)",
            "Index on top (with write and storage cost)",
            "Fix makes things worse",
            "Only unsafe fixes"]

_SIMULATED_INDEX = SAMPLE_RECOMMENDATIONS[3]
_UNSAFE_REWRITE = SAMPLE_RECOMMENDATIONS[0]


def demo_result(variant=VARIANTS[0]):
    """A full analyze()-shaped result built from the sample pieces."""
    r = {
        "query_id": "sample", "match": None, "leak_check": True,
        "masked_query": "SELECT c_1, SUM(c_9) FROM t_a3f WHERE c_5 > $1 AND c_91b = $2 GROUP BY c_1",
        "masked_plan": SAMPLE_PLAN, "bitmask": {"filter": "0110", "join": "1001", "group": "0001"},
        "bottleneck": SAMPLE_BOTTLENECK, "recommendations": list(SAMPLE_RECOMMENDATIONS),
        "cost_before": 19437.0, "cost_after": 812.0, "improvement_pct": 95.8,
        "explanation": "(sample) The big table is read in full. The measured rewrite is 3.5x faster.",
        "ai_inputs": {"time_fn": "used", "stats": "not available yet", "column_types": "not available yet"},
        "unsafe_rewrite": True,
    }
    if variant == VARIANTS[1]:       # an index on top: it has a real write and storage price
        r["recommendations"] = [dict(_SIMULATED_INDEX), SAMPLE_RECOMMENDATIONS[1], SAMPLE_RECOMMENDATIONS[2]]
        r.update(time_before_ms=418.0, time_after_ms=118.0,            # the winner, timed on a sandbox copy
                 write_latency_ms=0.35, storage_bytes=22_000_000, unsafe_rewrite=False,
                 explanation="(sample) The filter columns have no index. The index is much faster "
                             "but every insert pays a little and the index takes space.")
    elif variant == VARIANTS[2]:     # an honest bad result
        worse = {**_SIMULATED_INDEX, "cost_before": 1000.0, "cost_after": 1300.0, "improvement_pct": -30.0,
                 "reason": "The planner expects this index to be used, but it makes the query dearer."}
        r["recommendations"] = [worse]
        r.update(cost_before=1000.0, cost_after=1300.0, improvement_pct=-30.0, time_before_ms=100.0,
                 time_after_ms=140.0, write_latency_ms=0.9, storage_bytes=5_500_000, unsafe_rewrite=False,
                 explanation="(sample) This fix is estimated to make the query slower.")
    elif variant == VARIANTS[3]:     # nothing safe: no good news may be shown
        r["recommendations"] = [dict(_UNSAFE_REWRITE)]
        r.update(explanation="(sample) The only rewrite found returns different rows.")
    return r


if __name__ == "__main__":
    from app.ui_helpers import confidence_counts, confidence_strip, sort_recommendations, verified_badge
    for v in VARIANTS:
        r = demo_result(v)
        print(f"{v}: {confidence_strip(confidence_counts(r['recommendations']))}")
    for rec in sort_recommendations(demo_result()["recommendations"]):
        print(f"   {rec['kind']:10} {verified_badge(rec)}")
