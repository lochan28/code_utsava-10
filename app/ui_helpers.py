"""Small pure helpers for the dashboard (no Streamlit inside, so they are easy to test).

Everything here works on MASKED data only. Nothing here may call the decoder.
Every helper tolerates missing or odd input: it shows what exists and skips the rest.

Self-test:  .venv/Scripts/python -m app.ui_helpers
"""

# ============================================================
# FIXED SETTINGS (what the badges mean; changing these changes the demo's honesty)
# ============================================================
# How trustworthy a recommendation is, best first. "unsafe" is always last.
TIER_ORDER = ["measured", "simulated", "rule-only", "unverified", "unsafe"]
# tier -> (badge text, badge colour)
BADGES = {
    "measured": ("measured", "green"),                    # really run, same rows checked
    "simulated": ("cost-simulated", "blue"),              # HypoPG planner cost
    "rule-only": ("rule-based, not simulated", "gray"),
    "unverified": ("unverified", "gray"),                 # verified_by missing or unknown
    "unsafe": ("UNSAFE: results differ", "red"),
}
VERIFIED_BY_TO_TIER = {"timed_run": "measured", "hypopg_cost": "simulated", "rule_only": "rule-only"}
# How the bottleneck was found (P3's `path` values), in plain words.
PATH_LABELS = {"rules": "rule-based check", "gated_hybrid": "GNN, checked by rules", "gnn": "GNN"}
# The only things pipeline's `ai_inputs` may contain: status words, never data.
AI_INPUT_KEYS = ("time_fn", "stats", "column_types")
AI_INPUT_STATUSES = {"used", "not available yet", "not supported by ai/ yet",
                     "dropped: failed the leak check", "unavailable",
                     "failed: rewrites are unverified"}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def n(x):
    """12345.6 -> '12,346'; anything that is not a number -> '?'."""
    return f"{x:,.0f}" if _num(x) else "?"


def md_safe(text):
    """Escape '$' before text goes through st.markdown / st.write. Streamlit would read
    '$1 ... $2' as a maths formula and garble masked placeholders. (Code blocks are fine.)"""
    return str(text).replace("$", "\\$")


# ------------------------------------------------------------------
# Plan tree and match line
# ------------------------------------------------------------------
def plan_tree_text(masked_plan, highlight=None):
    """The masked plan as an indented tree, one step per line. `highlight` is the
    pre-order index of a step (P3's `node_id`) to mark as the bottleneck."""
    if isinstance(masked_plan, list):          # EXPLAIN (FORMAT JSON) wraps the plan in a list
        masked_plan = masked_plan[0] if masked_plan else {}
    root = masked_plan.get("Plan") if isinstance(masked_plan, dict) else None
    lines = []

    def walk(node, depth):
        where = f" on {node['Relation Name']}" if node.get("Relation Name") else ""
        mark = "   <== BOTTLENECK" if (_num(highlight) and highlight == len(lines)) else ""
        lines.append(f"{'   ' * depth}{'-> ' if depth else ''}{node.get('Node Type', '?')}{where}"
                     f"  (cost {n(node.get('Total Cost'))}, ~{n(node.get('Plan Rows'))} rows){mark}")
        for child in node.get("Plans", []):
            walk(child, depth + 1)

    if isinstance(root, dict):
        walk(root, 0)
    return "\n".join(lines) or "(no plan available)"


def match_line(m):
    """'Matched X (q11), 82% sure - method - runner-up' from pipeline's match dict."""
    if not m:
        return ""
    line = f"Matched **{m['name']}** ({m['query_id']}), {m['confidence']:.0%} sure · {m['method']}"
    return line + (f" · runner-up: {m['runner_up']}" if m.get("runner_up") else "")


# ------------------------------------------------------------------
# Bottleneck card
# ------------------------------------------------------------------
def bottleneck_items(b):
    """[(label, value)] for the bottleneck keys that EXIST, in display order."""
    if not isinstance(b, dict):
        return []
    items = []
    op = b.get("operator") or b.get("op")
    if op:
        items.append(("step", str(op)))
    if b.get("relation"):
        items.append(("table", str(b["relation"])))
    share = b.get("cost_share_pct")
    if not _num(share):                         # the stub reports a fraction inside `evidence`
        ev = b.get("evidence")
        frac = ev.get("cost_share") if isinstance(ev, dict) else None
        share = frac * 100 if _num(frac) else None
    if _num(share):
        items.append(("share of plan cost", f"{share:.0f}%"))
    if _num(b.get("rows")):
        items.append(("estimated rows", n(b["rows"])))
    if isinstance(b.get("path"), str):
        items.append(("found by", PATH_LABELS.get(b["path"], b["path"])))
    if isinstance(b.get("reason"), str) and b["reason"]:
        items.append(("why", b["reason"]))
    return items


def bottleneck_node_id(b):
    """The plan step to highlight, or None."""
    nid = b.get("node_id") if isinstance(b, dict) else None
    return nid if _num(nid) and nid >= 0 else None


# ------------------------------------------------------------------
# Recommendations: honesty badges, sorting, evidence
# ------------------------------------------------------------------
def is_unsafe(rec):
    """A rewrite that changes the result: flagged by the pipeline or by ai/ itself."""
    return rec.get("unsafe_rewrite") is True or rec.get("same_results") is False


def tier(rec):
    """measured / simulated / rule-only / unverified / unsafe (unsafe wins over the rest)."""
    if is_unsafe(rec):
        return "unsafe"
    v = rec.get("verified_by")
    return VERIFIED_BY_TO_TIER.get(v, "unverified") if isinstance(v, str) else "unverified"


def verified_badge(rec):
    """(text, colour) for the honesty badge. An unsafe rewrite shows ONLY the red one."""
    return BADGES[tier(rec)]


def novelty_badge(rec):
    """(text, colour) when the agent had not seen this kind of query, else None."""
    nv = rec.get("novelty")
    return ("unseen query shape", "orange") if isinstance(nv, str) and "unseen" in nv.lower() else None


def clean_recommendations(recs):
    """Only the dict items of a list; anything else becomes an empty list."""
    return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []


def sort_recommendations(recs):
    """Measured first, then simulated, rule-only, unverified; unsafe last. Stable inside a tier."""
    return sorted(clean_recommendations(recs), key=lambda r: TIER_ORDER.index(tier(r)))


def confidence_counts(recs):
    counts = dict.fromkeys(TIER_ORDER, 0)
    for r in clean_recommendations(recs):
        counts[tier(r)] += 1
    return counts


def confidence_strip(counts):
    """'2 measured · 1 simulated · 1 rule-only · 1 unsafe' (unverified only when there are some)."""
    parts = [f"{counts['measured']} measured", f"{counts['simulated']} simulated",
             f"{counts['rule-only']} rule-only"]
    if counts["unverified"]:
        parts.append(f"{counts['unverified']} unverified")
    parts.append(f"{counts['unsafe']} unsafe")
    return " · ".join(parts)


def evidence_items(rec):
    """[(label, text)]: the numbers and notes that back a recommendation. An empty list
    means NO evidence is attached, and the page must say so."""
    items = []
    ev = rec.get("evidence")
    if isinstance(ev, dict):
        items += [(str(k), str(v)) for k, v in ev.items() if v not in (None, "")]
    if _num(rec.get("cost_before")) and _num(rec.get("cost_after")):
        items.append(("planner cost", f"{n(rec['cost_before'])} → {n(rec['cost_after'])}"))
    if _num(rec.get("improvement_pct")):
        items.append(("estimated improvement", f"{rec['improvement_pct']:.0f}%"))
    if _num(rec.get("time_before_ms")) and _num(rec.get("time_after_ms")):
        items.append(("measured time", f"{n(rec['time_before_ms'])} ms → {n(rec['time_after_ms'])} ms"))
    if _num(rec.get("speedup")):
        items.append(("speedup", f"{rec['speedup']:.1f}x"))
    if isinstance(rec.get("verify_note"), str) and rec["verify_note"]:
        items.append(("check", rec["verify_note"]))
    return items


def ai_inputs_line(ai_inputs):
    """'Inputs given to the AI: time_fn: used · ...' - only known status words are shown,
    anything else is silently left out."""
    if not isinstance(ai_inputs, dict):
        return ""
    parts = [f"{k}: {ai_inputs[k]}" for k in AI_INPUT_KEYS
             if isinstance(ai_inputs.get(k), str) and ai_inputs[k] in AI_INPUT_STATUSES]
    return "Inputs given to the AI → " + " · ".join(parts) if parts else ""


if __name__ == "__main__":
    plan = {"Plan": {"Node Type": "Hash Join", "Total Cost": 20000.0, "Plan Rows": 900, "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "t_a3f", "Total Cost": 19437.0, "Plan Rows": 1200},
        {"Node Type": "Hash", "Total Cost": 50.0, "Plan Rows": 10, "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "t_b22", "Total Cost": 20.0, "Plan Rows": 10}]}]}}
    print(plan_tree_text(plan, highlight=1))
    recs = [{"kind": "rewrite", "unsafe_rewrite": True}, {"kind": "index", "verified_by": "hypopg_cost"},
            {"kind": "rewrite", "verified_by": "timed_run"}, {"kind": "partition", "verified_by": "rule_only"}]
    print([(r["kind"], verified_badge(r)) for r in sort_recommendations(recs)])
    print(confidence_strip(confidence_counts(recs)))
