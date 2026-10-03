"""The numbers behind the before/after view (Step 4c). Pure functions, no Streamlit.

Works on MASKED data only. Every number comes with the SOURCE it came from, and a number
that is missing is reported as missing: it is never defaulted to 0.

Self-test:  .venv/Scripts/python -m app.tradeoff
"""
import math

from app.ui_helpers import clean_recommendations, sort_recommendations, tier

# ============================================================
# TUNABLE SETTINGS
# ============================================================
TARGET_PCT = 50.0      # The problem statement asks for MORE than 50% improvement. The test is
                       # strictly "greater than", so exactly 50.0% does NOT count as met.
                       # Raise it to be stricter, lower it to be kinder.

# ============================================================
# FIXED SETTINGS (what the words on the page mean)
# ============================================================
BYTES_PER_MB = 1024 * 1024      # shown as "MB", the way Postgres' pg_size_pretty does
SOURCE_ESTIMATE = "planner estimate"       # HypoPG / EXPLAIN cost
SOURCE_MEASURED = "measured"               # really run on a sandbox copy
SOURCE_NONE = "not available"
NOT_MEASURED = "not measured"              # what the page shows instead of a missing number
NO_INDEX_NOTE = "none: a rewrite creates no index"


def ok(x):
    """A real, finite number (not None, not text, not True/False, not NaN)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def improvement(before, after):
    """% drop from before to after (95.8 = 96% lower). Negative = it got WORSE. None if unknowable."""
    if ok(before) and ok(after) and before > 0 and after >= 0:
        return (1 - after / before) * 100
    return None


def target_status(pct):
    """met (> TARGET_PCT) / below (0 < pct <= TARGET_PCT) / none (0) / worse (< 0) / unknown."""
    if not ok(pct):
        return "unknown"
    if pct > TARGET_PCT:
        return "met"
    if pct > 0:
        return "below"
    return "none" if pct == 0 else "worse"


def disagree(cost, time):
    """True when the planner estimate and the measured time point in OPPOSITE directions
    (one says better, the other says worse). Zero or unknown never counts as a disagreement."""
    if not (cost and time and ok(cost["pct"]) and ok(time["pct"])):
        return False
    return cost["pct"] * time["pct"] < 0


def top_recommendation(recs):
    """The fix the page calls 'top pick': first by trust, never an unsafe one. None if there is none."""
    ranked = sort_recommendations(recs)
    return ranked[0] if ranked and tier(ranked[0]) != "unsafe" else None


def _pair(before, after, source, pct=None):
    pct = pct if ok(pct) else improvement(before, after)       # ai/'s own measured_gain_pct wins when it gives one
    return {"before": before, "after": after, "pct": pct, "status": target_status(pct), "source": source}


def _own_or_inherited(top, r, key, inherit):
    """The top fix's own number if it has one, else the result-level number, but only when
    that result-level number is about THIS fix (it is the pipeline's best, the first listed)."""
    if ok(top.get(key)):
        return top[key]
    return r.get(key) if inherit and ok(r.get(key)) else None


def resolve(r):
    """Everything the before/after view needs, for the top pick.

    Returns {"top", "no_safe", "cost", "time", "write", "write_note", "storage", "storage_note"}:
      no_safe   True when there ARE recommendations but every one is unsafe: show only a warning
      cost      {"before","after","pct","status","source"} (planner estimate) or None
      time      same shape, source "measured", or None (then: 'not measured, cost estimate only')
      write     ms per insert (measured) or None ;  storage  bytes (planner estimate) or None
      *_note    why a number is absent by design (a rewrite creates no index, so nothing to pay)
    """
    recs = clean_recommendations(r.get("recommendations"))
    top = top_recommendation(recs)
    out = {"top": top, "no_safe": bool(recs) and top is None, "cost": None, "time": None,
           "write": None, "write_note": None, "storage": None, "storage_note": None, "rejected": []}
    # fixes ai/ dropped because they returned different results (masked text, a few short strings)
    rejected = (top or (recs[0] if recs else {})).get("rejected_fixes")
    if isinstance(rejected, list):
        out["rejected"] = [x for x in rejected if isinstance(x, str)][:5]
    if out["no_safe"]:
        return out
    # With no recommendations at all, the result-level numbers have no one to contradict them.
    # With some, they describe the pipeline's best = the first listed, so only that fix may use them.
    inherit = (not recs) or (top is recs[0])
    t = top or {}

    before = _own_or_inherited(t, r, "cost_before", inherit)
    after = _own_or_inherited(t, r, "cost_after", inherit)
    if ok(before) and ok(after):
        out["cost"] = _pair(before, after, SOURCE_ESTIMATE)

    own = [(t.get(b), t.get(a)) for b, a in (("measured_ms_before", "measured_ms_after"),
                                              ("time_before_ms", "time_after_ms"))]
    own = next(((b, a) for b, a in own if ok(b) and ok(a)), None)
    if tier(t) == "measured" and own:
        out["time"] = _pair(own[0], own[1], SOURCE_MEASURED, t.get("measured_gain_pct"))
        if isinstance(t.get("timing_note"), str) and t["timing_note"]:
            out["time"]["note"] = t["timing_note"]
    elif inherit and ok(r.get("time_before_ms")) and ok(r.get("time_after_ms")):
        out["time"] = _pair(r["time_before_ms"], r["time_after_ms"], SOURCE_MEASURED)

    if t.get("kind") == "rewrite":                   # a rewrite builds no index: nothing to pay
        out["write_note"] = out["storage_note"] = NO_INDEX_NOTE
    else:
        out["write"] = _own_or_inherited(t, r, "write_latency_ms", inherit)
        out["storage"] = _own_or_inherited(t, r, "storage_bytes", inherit)
    return out


# ------------------------------------------------------------------
# Text for the page
# ------------------------------------------------------------------
def fmt_pct(p):
    return f"{p:.1f}%" if ok(p) else NOT_MEASURED


def fmt_ms(x):
    return f"{x:,.0f} ms" if x >= 10 else f"{x:.1f} ms"


def fmt_write(ms):
    return f"{ms:+.3g} ms per insert" if ok(ms) else NOT_MEASURED


def fmt_storage(b):
    return f"+{b / BYTES_PER_MB:.1f} MB" if ok(b) else NOT_MEASURED


if __name__ == "__main__":
    for before, after in ((19437, 812), (200, 100), (100, 120), (0, 5), (None, 3)):
        p = improvement(before, after)
        print(f"{before!s:>6} -> {after!s:<4} improvement={fmt_pct(p):>9}  status={target_status(p)}")
