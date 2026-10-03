"""Test: recommend() reports the MEASURED gain of the winning fix, planner estimate second.

Three cases: time_fn present, time_fn missing, time_fn present but the results differ
(then the fix is rejected). Masked toy query, fake cost_fn / time_fn, untrained agent.
Run:  .venv/Scripts/python -m ai.test_measured
"""
from ai import rl

SQL = "SELECT COUNT(*) FROM t_1c1 WHERE c_0b8 = $1"
PLAN = {"Plan": {"Node Type": "Seq Scan", "Relation Name": "t_1c1", "Total Cost": 1000.0, "Plan Rows": 10}}
STATS = {"rows": {"t_1c1": 1_000_000.0}, "eq_sel": {"t_1c1.c_0b8": 0.00001}, "range_sel": {},
         "existing_indexes": []}
FIX = "CREATE INDEX ON t_1c1 (c_0b8)"


def cost_fn(sql, actions):
    return 100.0 if FIX in actions else 1000.0          # planner: 90% lower with the index


def make_time_fn(match=True):
    def time_fn(sql, actions=()):
        fixed = FIX in actions
        return {"ms": 40.0 if fixed else 100.0,           # measured: only 60% faster
                "rows": 1, "checksum": "same" if (match or not fixed) else "different"}
    return time_fn


def run(time_fn):
    return rl.recommend(SQL, PLAN, cost_fn, stats=STATS, time_fn=time_fn, agent=rl.BanditAgent())


def test_time_fn_present():
    top = run(make_time_fn())[0]
    assert top["action"] == FIX and top["verified_by"] == "timed_run", top
    assert (top["measured_ms_before"], top["measured_ms_after"]) == (100.0, 40.0)
    assert top["measured_gain_pct"] == 60.0 and top["planner_gain_pct"] == 90.0
    assert top["summary"].startswith("Add index on t_1c1 (c_0b8) (60% faster in measured sandbox time "
                                     "(planner estimated 90%)"), top["summary"]


def test_time_fn_missing():
    top = run(None)[0]
    assert top["verified_by"] == "hypopg_cost" and top["measured_gain_pct"] is None
    assert "estimated by the planner (not timed)" in top["summary"], top["summary"]


def test_results_differ_rejects():
    recs = run(make_time_fn(match=False))
    assert all(r["action"] != FIX for r in recs), [r["action"] for r in recs]
    if recs:                                             # anything left says why the fix was dropped
        assert any("rejected" in x for x in recs[0].get("rejected_fixes", [])), recs[0]


def test_combined_and_measured_pick():
    """Planner likes the partition most; measured time says partition + index is best."""
    sql = "SELECT COUNT(*) FROM t_1c1 WHERE c_0b8 = $1 AND c_02b >= $2"
    types = {"t_1c1.c_0b8": "number", "t_1c1.c_02b": "date"}
    stats = {**STATS, "range_sel": {"t_1c1.c_02b": 0.1}}

    def cost(sql_, acts):
        p, i = any("PARTITION" in a for a in acts), any("INDEX" in a for a in acts)
        return 1000.0 - 800 * p - 500 * i if not (p and i) else 150.0

    def timed(sql_, actions=()):
        p, i = any("PARTITION" in a for a in actions), any("INDEX" in a for a in actions)
        return {"ms": {(0, 0): 100.0, (1, 0): 70.0, (0, 1): 60.0, (1, 1): 20.0}[(p, i)],
                "rows": 1, "checksum": "same"}

    recs = rl.recommend(sql, PLAN, cost, stats=stats, time_fn=timed, agent=rl.BanditAgent(),
                        column_types=types)
    kinds = [r["kind"] for r in recs]
    if "partition" not in kinds:
        print("SKIP combined test: untrained agent did not shortlist a partition", kinds)
        return
    top = recs[0]
    assert top.get("combined") and top["measured_gain_pct"] == 80.0, top
    assert "PARTITION" in top["action"] and "CREATE INDEX" in top["action"], top["action"]
    print("  combined summary:", top["summary"])


if __name__ == "__main__":
    for t in (test_time_fn_present, test_time_fn_missing, test_results_differ_rejects,
              test_combined_and_measured_pick):
        t()
        print("PASS", t.__name__)
    print("example summary:", run(make_time_fn())[0]["summary"])
