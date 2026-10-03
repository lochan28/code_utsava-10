"""End-to-end pipeline: question in -> masked analysis -> recommendation out.

Owner: Person 4.  Works today with the stub modules; gets real as each
module is filled in.  The privacy boundary is HERE: real SQL and data stay
on the left (db/, privacy/), only masked data crosses to ai/ and app/llm.py.

Self-test:  .venv/Scripts/python pipeline.py
"""
import json

from ai import gnn, rl
from app import llm
from db import queries, sandbox
from privacy import mask

# ============================================================
# TUNABLE SETTINGS
# ============================================================
DEFAULT_QUERY_ID = "q11"   # used when the question matches no query name


def pick_query(question):
    """Match the admin's question to one of our known slow queries by shared words."""
    words = set(question.lower().replace("?", "").split())
    best = max(queries.QUERIES, key=lambda q: len(words & set(q["name"].lower().split())))
    if not words & set(best["name"].lower().split()):
        best = queries.get(DEFAULT_QUERY_ID)
    return best


def analyze(question):
    q = pick_query(question)
    mapping = mask.build_mapping()                      # private, stays here

    # ---- real side: only db/ and privacy/ see real SQL ----
    masked_sql = mask.mask_query(q["sql"], mapping)
    masked_plan = mask.mask_plan(sandbox.get_plan(q["sql"]), mapping)
    bitmask = mask.column_bitmask(q["sql"], mapping)

    def cost_fn(m_sql, m_indexes):
        """The AI's only window into the DB: masked SQL in, a number out."""
        return sandbox.cost(q["sql"], [mask.unmask(i, mapping) for i in m_indexes])

    # ---- AI side: masked data only ----
    bottleneck = gnn.find_bottleneck(masked_plan)
    recs = rl.recommend(masked_sql, masked_plan, cost_fn)
    best = recs[0]
    masked_result = {"masked_query": masked_sql, "masked_plan": masked_plan,
                     "bitmask": bitmask, "bottleneck": bottleneck, "recommendations": recs}
    explanation = llm.explain(masked_result)

    # Everything the AI saw or produced must be free of real names/values.
    leak_check = mask.verify_no_leak(json.dumps(masked_result) + explanation, mapping)

    # ---- back on the real side: un-mask only for the admin's screen ----
    deploy_sql = mask.unmask(best["action"], mapping)
    t_before = sandbox.timed_run(q["sql"])
    t_after = sandbox.timed_run(q["sql"], [deploy_sql])
    return {
        "query_id": q["id"],
        "masked_query": masked_sql,
        "masked_plan": masked_plan,
        "bitmask": bitmask,
        "bottleneck": bottleneck,
        "recommendations": recs,
        "cost_before": best["cost_before"],
        "cost_after": best["cost_after"],
        "improvement_pct": round(100 * (1 - best["cost_after"] / best["cost_before"]), 1),
        "time_before_ms": t_before,
        "time_after_ms": t_after,
        "write_latency_ms": sandbox.measure_write_latency(deploy_sql)["delta_ms"],
        "storage_bytes": sandbox.index_size_bytes(deploy_sql),
        "explanation": explanation,
        "deploy_sql": deploy_sql,
        "leak_check": leak_check,
    }


if __name__ == "__main__":
    result = analyze("Why is the weekly sales report slow?")
    for k, v in result.items():
        print(f"{k:16} {json.dumps(v)[:100]}")
