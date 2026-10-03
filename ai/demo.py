"""One-command demo of the ai/ flow on the problem statement's example (q11).

Owner: Person 3.  Masked data only. Uses the SAME find_bottleneck() and recommend()
as the pipeline.

    .venv/Scripts/python -m ai.demo

Where the database numbers come from:
  - LIVE: if the pipeline hands in cost_fn / time_fn (call run(cost_fn=..., time_fn=...)),
    they are used directly.
  - RECORDED (default from the command line): ai/ cannot reach the database, so cost_fn
    REPLAYS real HypoPG costs recorded from a real run (ai/demo_q11_recorded.json, made by
    Person 3's p3_record_demo.py outside ai/). Every replayed number is labelled "recorded".
"""
import json
from pathlib import Path

from ai import gnn, rl

# ============================================================
# FIXED SETTINGS
# ============================================================
RECORDED = Path(__file__).with_name("demo_q11_recorded.json")


def recorded_cost_fn(rec):
    """Replay the recorded real costs: same (masked SQL, masked actions) -> same number."""
    table = {(c["sql"], tuple(c["actions"])): c for c in rec["cost_calls"]}

    def cost_fn(masked_sql, masked_actions):
        hit = table.get((masked_sql, tuple(masked_actions)))
        if hit is None:
            raise KeyError("not in the recording - run live, or re-record with p3_record_demo.py")
        if "error" in hit:
            raise ValueError("recorded: the sandbox could not simulate this action")
        return hit["cost"]
    return cost_fn


def run(cost_fn=None, time_fn=None, stats=None, out=print):
    rec = json.loads(RECORDED.read_text(encoding="utf-8"))
    live = cost_fn is not None
    cost_fn = cost_fn or recorded_cost_fn(rec)
    stats = stats or {k: (v if k != "existing_indexes" else [(t, tuple(c)) for t, c in v])
                      for k, v in rec["stats"].items()}
    source = "LIVE (pipeline cost_fn)" if live else f"RECORDED real run {rec['recorded_at']} ({rec['source']})"

    out("=" * 78)
    out(f"QUESTION   {rec['question']}   ->  matched query {rec['query_id']}")
    out(f"DB NUMBERS {source}")
    out(f"MASKED SQL {rec['masked_sql'][:150]}...")
    out("-" * 78)

    b = gnn.find_bottleneck(rec["masked_plan"])
    out(f"BOTTLENECK node {b['node_id']} = {b['operator']} on {b['relation']}   (path: {b['path']})")
    out(f"           {b['reason']}")
    out("-" * 78)

    recs = rl.recommend(rec["masked_sql"], rec["masked_plan"], cost_fn, stats=stats, time_fn=time_fn)
    top = recs[0]
    tag = "" if live else "   [recorded]"
    out(f"TOP FIX    {top['action']}")
    out(f"           cost {top['cost_before']:,.0f} -> {top['cost_after']:,.0f}  "
        f"({top['improvement_pct']}% lower){tag}")
    out(f"VERIFIED   {top['verified_by']}{tag}")
    out(f"NOVELTY    {top['novelty']}")
    out(f"WHY NOT    {top.get('why_not', '-')}")
    out(f"SUMMARY    {top['summary']}")
    out("=" * 78)
    out("Names are masked: the pipeline un-masks them for the admin (see ai/RESULTS.md section 6).")
    return {"bottleneck": b, "recommendations": recs, "live": live}


if __name__ == "__main__":
    run()
