"""One evidence-based sentence on why a candidate fix was REJECTED.

Owner: Person 3.  Masked names only; the pipeline un-masks the sentence for display.
Built from the same numbers the agent used (selectivity, distinct values, table
size, driving rows) plus what was actually measured, so every "no" has a reason.

Self-test:  .venv/Scripts/python -m ai.why_not
"""

# ============================================================
# TUNABLE SETTINGS - thresholds for which explanation is chosen
# ============================================================
BROAD_SELECTIVITY = 0.02    # An exact-match column matching >= 2% of rows per value is
                            # "too broad": the planner would rather scan.
SMALL_TABLE_ROWS = 10_000   # Tables smaller than this are cheap to scan anyway.
BROAD_RANGE = 0.3           # A range covering >= 30% of rows gains little from partitions.


def _pct(x):
    return f"{100 * x:.1f}%" if x < 0.1 else f"{100 * x:.0f}%"


def why_not(c, info, stats, measured=None, chosen=None, pairs=()):
    """c: the rejected candidate. measured: {"drop": 0..1, "reward": float} or a rewrite
    verification dict (ai.verify), or None if it was never simulated.
    chosen: {"drop": 0..1, "label": str} for the winning fix, for comparison.
    pairs: join pairs of the query (ai.sqlinfo.join_pairs), for join-index reasons."""
    from ai import rl          # local import: rl imports this module

    label = c.get("rule") or " + ".join(c["sqls"])
    vs = f" vs {_pct(chosen['drop'])} for the chosen fix" if chosen else ""

    # --- rewrites: judged by real results first
    if c["kind"] == "rewrite":
        if measured and measured.get("verified_by") == "timed_run":
            if not measured["same_results"]:
                return f"Rewrite '{label}' rejected: it returned different rows than the original query."
            if measured["accepted"]:
                return (f"Rewrite '{label}' also works (same rows, {measured['time_before_ms']:,.0f} ms -> "
                        f"{measured['time_after_ms']:,.0f} ms, {measured['speedup']:.1f}x faster) but the "
                        f"chosen fix gains more ({_pct(1 - 1 / measured['speedup'])}{vs}). Note: rewrites are "
                        f"measured in real time, indexes by planner cost.")
            return (f"Rewrite '{label}' rejected: same rows but not faster when run "
                    f"({measured['time_before_ms']:,.0f} ms -> {measured['time_after_ms']:,.0f} ms).")
        return f"Rewrite '{label}' not chosen: it was not run for real (no time_fn), so it stays unverified."

    (table, cols), = c["cols"].items()
    rows = stats["rows"].get(table, 0)
    col = cols[0]
    key = f"{table}.{col}"
    roles = set(info["columns"].get(key, {"roles": []})["roles"])
    sim = f" (simulated cost drop {_pct(measured['drop'])}{vs})" if measured and "drop" in measured else ""

    if c["kind"] == "partition":
        rsel = stats["range_sel"].get(key)
        if rsel is not None and rsel >= BROAD_RANGE:
            return (f"Partitioning {table} by {col} rejected: the query's range covers about "
                    f"{_pct(rsel)} of the rows, so partitions would skip little{sim}.")
        return f"Partitioning {table} by {col} not chosen: a smaller change does as well or better{sim}."

    if rows and rows < SMALL_TABLE_ROWS:
        return (f"Index on {table}({', '.join(cols)}) rejected: the table has only about {rows:,.0f} rows, "
                f"so scanning it is already cheap{sim}.")
    if len(cols) == 1 and ("eq_filter" in roles or "or_filter" in roles):   # distinct-values reason: 1 column only
        sel = stats["eq_sel"].get(key)
        if sel is not None and sel >= BROAD_SELECTIVITY:
            return (f"Index on {table}({col}) rejected: {col} has only about {round(1 / sel):,} distinct values "
                    f"({_pct(sel)} of rows per value), so the planner would still read much of the table{sim}.")
    if roles & rl.JOIN_ROLES:
        ratio = rl.driving_ratio(table, col, info, stats, pairs)
        if ratio is not None and ratio >= 0.05:
            return (f"Index on {table}({col}) rejected: about {ratio * rows:,.0f} rows would look it up, "
                    f"so hashing the whole table for the join stays cheaper{sim}.")
    width = rl.write_width(c)
    if measured and "drop" in measured and measured["drop"] <= 0:
        return (f"Index on {table}({', '.join(cols)}) rejected: the planner would not use it - "
                f"the simulated cost does not go down at all.")
    if chosen and measured and "drop" in measured and measured["drop"] < chosen["drop"]:
        return (f"Index on {table}({', '.join(cols)}) not chosen: it cuts the simulated cost by only "
                f"{_pct(measured['drop'])}{vs}.")
    if measured and "drop" in measured and measured["drop"] > 0 and measured.get("reward", 1) < measured["drop"]:
        return (f"Index on {table}({', '.join(cols)}) not chosen: it would cut cost by {_pct(measured['drop'])} "
                f"but adds a {width}-column index that every insert and update must maintain{vs}.")
    return f"Index on {table}({', '.join(cols)}) not chosen{sim or ': it ranked lower and was not simulated'}."
