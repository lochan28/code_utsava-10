"""Test of the range_sel fallback (CLAUDE.md: missing range_sel -> estimate from masked plan).

Run:  .venv/Scripts/python -m ai.test_range_sel
"""
from ai import rl, sqlinfo

SQL = "SELECT COUNT(*) FROM t_1c1 WHERE c_0b8 = $1 AND c_02b >= $2"
PLAN = {"Plan": {"Node Type": "Aggregate", "Total Cost": 100.0, "Plan Rows": 1, "Plans": [
    {"Node Type": "Seq Scan", "Relation Name": "t_1c1", "Parent Relationship": "Outer",
     "Total Cost": 90.0, "Plan Rows": 300}]}}
BASE = {"rows": {"t_1c1": 100_000.0}, "eq_sel": {"t_1c1.c_0b8": 0.1}, "existing_indexes": []}


def test_fills_missing():
    stats = rl.normalize_stats(dict(BASE, range_sel={}))
    filled = rl.fill_range_sel(stats, sqlinfo.analyze(SQL), PLAN)
    # 300 rows out of 100,000 = 0.003, divided by the exact-match selectivity 0.1 -> 0.03
    assert filled == ["t_1c1.c_02b"], filled
    assert abs(stats["range_sel"]["t_1c1.c_02b"] - 0.03) < 1e-9, stats["range_sel"]


def test_keeps_given_value():
    given = {"t_1c1.c_02b": 0.5}
    stats = rl.normalize_stats(dict(BASE, range_sel=given))
    assert rl.fill_range_sel(stats, sqlinfo.analyze(SQL), PLAN) == []
    assert stats["range_sel"]["t_1c1.c_02b"] == 0.5 and given == {"t_1c1.c_02b": 0.5}   # caller untouched


def test_parallel_scan_counts_workers():
    plan = {"Plan": {"Node Type": "Gather", "Workers Planned": 2, "Total Cost": 100.0, "Plan Rows": 300,
                     "Plans": [dict(PLAN["Plan"]["Plans"][0], **{"Parallel Aware": True, "Plan Rows": 100})]}}
    stats = rl.normalize_stats(dict(BASE, range_sel={}))
    rl.fill_range_sel(stats, sqlinfo.analyze(SQL), plan)
    assert abs(stats["range_sel"]["t_1c1.c_02b"] - 0.03) < 1e-9     # 100 per worker x 3 processes


def test_no_plan_no_change():
    stats = rl.normalize_stats(dict(BASE, range_sel={}))
    assert rl.fill_range_sel(stats, sqlinfo.analyze(SQL), None) == [] and stats["range_sel"] == {}


if __name__ == "__main__":
    for t in (test_fills_missing, test_keeps_given_value, test_parallel_scan_counts_workers, test_no_plan_no_change):
        t()
        print("PASS", t.__name__)
