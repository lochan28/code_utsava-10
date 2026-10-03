"""Judge SQL rewrites by REAL time and SAME results - not by planner cost.

Owner: Person 3.  Sees ONLY masked SQL; the real run happens behind time_fn.

Why: step 5 showed the planner's cost estimate can be badly wrong for
rewrites (NOT IN -> NOT EXISTS was ESTIMATED 23% more expensive but MEASURED
3.5x faster). So a rewrite is accepted only if it (1) returns exactly the
same rows and (2) is actually faster when run.

time_fn contract (provided by the pipeline, Person 4; real run by Person 1).
verify_rewrite() calls it twice: first the ORIGINAL query, then the REWRITE.
Two call styles:
  - two-argument (preferred, if time_fn has a `compare_to` parameter):
       time_fn(rewritten_sql, compare_to=original_sql)
    the pipeline compares the rewrite against exactly that original - no hidden state.
  - one-argument (fallback): time_fn(rewritten_sql); "results_match" then means
    "same as the query from the previous call" (call order matters).
Two return shapes are accepted:
  A) P4 (preferred, nothing derived from real rows reaches ai/):
       time_fn(masked_sql) -> {"ms": float, "results_match": bool}
     "results_match" on the SECOND call = the rewrite returned exactly the same
     rows as the original from the first call (compared inside the pipeline).
  B) Contract v2 (CLAUDE.md, P1 checked_run):
       time_fn(masked_sql) -> {"ms": float, "rows": int, "checksum": str}
     compared here (row count + order-independent checksum).
Without time_fn, rewrites can only be checked by the planner: verified_by="rule_only".

Self-test:  .venv/Scripts/python -m ai.verify
"""

import inspect

# ============================================================
# TUNABLE SETTINGS
# ============================================================
MIN_SPEEDUP = 1.05   # A rewrite must be at least this much faster (x) to count as better.
                     # Higher = only accept clear wins; 1.0 = accept any measured gain.


def _accepts_compare_to(fn):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "compare_to" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def verify_rewrite(masked_sql, rewritten_sql, time_fn=None):
    """Run original and rewritten query through time_fn and compare.

    Returns {"verified_by", "same_results", "time_before_ms", "time_after_ms",
             "speedup", "accepted", "note"}."""
    if time_fn is None:
        return {"verified_by": "rule_only", "same_results": None, "time_before_ms": None,
                "time_after_ms": None, "speedup": None, "accepted": None,
                "note": "not run: no time_fn, judged by the rewrite rule only"}
    before = time_fn(masked_sql)
    if _accepts_compare_to(time_fn):
        after = time_fn(rewritten_sql, compare_to=masked_sql)
    else:
        after = time_fn(rewritten_sql)
    if "results_match" in after:            # shape A: the pipeline already compared the rows
        same = bool(after["results_match"])
    else:                                   # shape B: compare count + checksum here
        same = before["rows"] == after["rows"] and before["checksum"] == after["checksum"]
    speedup = before["ms"] / after["ms"] if after["ms"] > 0 else float("inf")
    accepted = same and speedup >= MIN_SPEEDUP
    if not same:
        note = "different results from the original query - rejected"
    elif not accepted:
        note = f"same results but not faster ({before['ms']:,.0f} ms -> {after['ms']:,.0f} ms)"
    else:
        note = f"same results, {before['ms']:,.0f} ms -> {after['ms']:,.0f} ms ({speedup:.1f}x faster)"
    return {"verified_by": "timed_run", "same_results": same, "time_before_ms": round(before["ms"], 1),
            "time_after_ms": round(after["ms"], 1), "speedup": round(speedup, 2),
            "accepted": accepted, "note": note}


if __name__ == "__main__":
    # A pretend time_fn: rewrite 1 is equal and faster, rewrite 2 changes the result.
    fake = {"orig": {"ms": 418.0, "rows": 108, "checksum": "a1"},
            "good": {"ms": 118.0, "rows": 108, "checksum": "a1"},
            "bad": {"ms": 90.0, "rows": 107, "checksum": "b7"}}
    tf = lambda sql: fake[sql]
    print("B good rewrite:", verify_rewrite("orig", "good", tf))
    print("B bad rewrite: ", verify_rewrite("orig", "bad", tf))
    # style 2: one-argument, shape A (ms + results_match), relies on call order
    fake_a = {"orig": {"ms": 418.0, "results_match": True}, "good": {"ms": 118.0, "results_match": True},
              "bad": {"ms": 90.0, "results_match": False}}
    ta = lambda sql: fake_a[sql]
    print("A good rewrite:", verify_rewrite("orig", "good", ta))
    print("A bad rewrite: ", verify_rewrite("orig", "bad", ta))

    # style 3: two-argument, shape A - the pipeline compares against compare_to explicitly
    same_rows = {"orig": "R1", "good": "R1", "bad": "R2"}          # pretend result sets
    calls = []

    def tc(sql, compare_to=None):
        calls.append((sql, compare_to))
        match = None if compare_to is None else same_rows[sql] == same_rows[compare_to]
        return {"ms": fake_a[sql]["ms"], "results_match": match}

    print("C good rewrite:", verify_rewrite("orig", "good", tc))
    print("C bad rewrite: ", verify_rewrite("orig", "bad", tc))
    print("C calls made:  ", calls)
    print("no time_fn:    ", verify_rewrite("orig", "good"))
