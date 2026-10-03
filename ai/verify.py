"""Judge SQL rewrites by REAL time and SAME results - not by planner cost.

Owner: Person 3.  Sees ONLY masked SQL; the real run happens behind time_fn.

Why: step 5 showed the planner's cost estimate can be badly wrong for
rewrites (NOT IN -> NOT EXISTS was ESTIMATED 23% more expensive but MEASURED
3.5x faster). So a rewrite is accepted only if it (1) returns exactly the
same rows and (2) is actually faster when run.

time_fn contract (provided by the pipeline, Person 4; real run by Person 1):
    time_fn(masked_sql) -> {"ms": float, "rows": int, "checksum": str}
  - un-masks the SQL privately, runs it on the sandbox copies (read-only),
  - "ms": median execution time, "rows": row count,
  - "checksum": order-independent hash of all result rows, e.g. on Postgres:
      SELECT count(*), md5(string_agg(md5(t::text), '' ORDER BY md5(t::text)))
      FROM (<query>) AS t
Without time_fn, rewrites can only be checked by the planner: verified_by="rule_only".

Self-test:  .venv/Scripts/python -m ai.verify
"""

# ============================================================
# TUNABLE SETTINGS
# ============================================================
MIN_SPEEDUP = 1.05   # A rewrite must be at least this much faster (x) to count as better.
                     # Higher = only accept clear wins; 1.0 = accept any measured gain.


def verify_rewrite(masked_sql, rewritten_sql, time_fn=None):
    """Run original and rewritten query through time_fn and compare.

    Returns {"verified_by", "same_results", "time_before_ms", "time_after_ms",
             "speedup", "accepted", "note"}."""
    if time_fn is None:
        return {"verified_by": "rule_only", "same_results": None, "time_before_ms": None,
                "time_after_ms": None, "speedup": None, "accepted": None,
                "note": "not run: no time_fn, judged by the rewrite rule only"}
    before = time_fn(masked_sql)
    after = time_fn(rewritten_sql)
    same = before["rows"] == after["rows"] and before["checksum"] == after["checksum"]
    speedup = before["ms"] / after["ms"] if after["ms"] > 0 else float("inf")
    accepted = same and speedup >= MIN_SPEEDUP
    if not same:
        note = (f"different results: {before['rows']:,} rows before vs {after['rows']:,} after "
                f"(or different values) - rejected")
    elif not accepted:
        note = f"same results but not faster ({before['ms']:,.0f} ms -> {after['ms']:,.0f} ms)"
    else:
        rows = f"{after['rows']:,} row" + ("" if after["rows"] == 1 else "s")
        note = f"same {rows}, {before['ms']:,.0f} ms -> {after['ms']:,.0f} ms ({speedup:.1f}x faster)"
    return {"verified_by": "timed_run", "same_results": same, "time_before_ms": round(before["ms"], 1),
            "time_after_ms": round(after["ms"], 1), "speedup": round(speedup, 2),
            "accepted": accepted, "note": note}


if __name__ == "__main__":
    # A pretend time_fn: rewrite 1 is equal and faster, rewrite 2 changes the result.
    fake = {"orig": {"ms": 418.0, "rows": 108, "checksum": "a1"},
            "good": {"ms": 118.0, "rows": 108, "checksum": "a1"},
            "bad": {"ms": 90.0, "rows": 107, "checksum": "b7"}}
    tf = lambda sql: fake[sql]
    print("good rewrite:", verify_rewrite("orig", "good", tf))
    print("bad rewrite: ", verify_rewrite("orig", "bad", tf))
    print("no time_fn:  ", verify_rewrite("orig", "good"))
