"""Streamlit dashboard.  Owner: Person 4.  STATUS: Step 4b (chat, badges, masked query
and plan, bottleneck card, recommendations with honesty badges).  4c adds charts and
4d the Approve script.

EVERYTHING on this page is MASKED data. This file never calls the decoder (unmask):
the un-masked version belongs to the Approve step only.

Run:  .venv/Scripts/streamlit run app/dashboard.py
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "import pipeline" works
import pipeline  # noqa: E402
from app import llm, sample_data  # noqa: E402
from app.ui_helpers import (  # noqa: E402
    ai_inputs_line, bottleneck_items, bottleneck_node_id, confidence_counts, confidence_strip,
    evidence_items, match_line, md_safe, novelty_badge, plan_tree_text, sort_recommendations,
    tier, verified_badge)

# ============================================================
# TUNABLE SETTINGS
# ============================================================
BADGE_REFRESH = "3s"   # How often the "LLM status" badge re-checks on its own, so it turns
                       # green when the model finishes loading. Lower = snappier, a little
                       # more work in the background.
PAGE_TITLE = "AI Database Tuner"

st.set_page_config(page_title=PAGE_TITLE, page_icon=":material/database:", layout="wide")
llm.start_warmup()   # load the model in the background at app start; only the first call acts


# ------------------------------------------------------------------
# Header: LLM status badge (refreshes itself)
# ------------------------------------------------------------------
@st.fragment(run_every=BADGE_REFRESH)
def llm_badge():
    s = llm.llm_status()
    color = {"live LLM": "green", "warming up": "orange"}.get(s["status"], "gray")
    st.badge(f"LLM: {s['status']} · {s['model']}", icon=":material/smart_toy:", color=color,
             help=s["reason"])
    if s["status"] != "live LLM":
        st.caption(s["reason"])


head_left, head_right = st.columns([3, 1], vertical_alignment="center")
head_left.title(PAGE_TITLE)
head_left.caption("Finds slow queries and suggests fixes. The AI only ever sees masked data.")
with head_right:
    llm_badge()


MASKED_NOTE = ("Masked view: table and column names are hashed and values are replaced. "
               "The un-masked version is only shown in the Approve step.")


# ------------------------------------------------------------------
# Bottleneck card
# ------------------------------------------------------------------
def render_bottleneck(b):
    items = bottleneck_items(b)           # only the keys that exist; never crashes
    with st.container(border=True):
        st.markdown("**Bottleneck** · the step that costs the most")
        if not items:
            st.caption("No bottleneck details available.")
            return
        d = dict(items)
        st.markdown("#### " + md_safe(d.get("step", "Plan step") + (f" on {d['table']}" if "table" in d else "")))
        facts = [f"{k}: **{md_safe(v)}**" for k, v in items if k not in ("step", "table", "why", "found by")]
        if facts:
            st.markdown("  ·  ".join(facts))
        if "found by" in d:
            st.badge(f"found by: {d['found by']}", icon=":material/search:",
                     color="violet" if "GNN" in d["found by"] else "gray")
        if "why" in d:
            st.markdown(md_safe(d["why"]))


# ------------------------------------------------------------------
# Recommendations: one bordered row each, with honesty badges
# ------------------------------------------------------------------
REC_COLUMNS = [1.3, 3, 3, 2.6]      # type | fix | reason | evidence


def render_rec(rec, top):
    t = tier(rec)
    with st.container(border=True):
        if t == "unsafe":             # loud, and impossible to read as a good fix
            warning = rec.get("warning") if isinstance(rec.get("warning"), str) else \
                "This rewrite returns different rows than the original query. Do not deploy."
            st.error(f"**UNSAFE: results differ.** {md_safe(warning)}", icon=":material/dangerous:")
        c_type, c_fix, c_reason, c_ev = st.columns(REC_COLUMNS)
        with c_type:
            st.markdown(f"**{md_safe(str(rec.get('kind') or '?').upper())}**")
            label, color = verified_badge(rec)
            st.badge(label, color=color)
            nb = novelty_badge(rec)
            if nb:
                st.badge(nb[0], color=nb[1])
            if top:
                st.badge("top pick", icon=":material/star:", color="green")
        with c_fix:
            action = rec.get("action")
            if isinstance(action, str) and action.strip():
                st.code(action, language="sql")
            else:
                st.caption("No fix text (the masked SQL could not be analysed).")
        with c_reason:
            reason = rec.get("reason")
            st.markdown(md_safe(reason) if isinstance(reason, str) and reason else "No reason given.")
        with c_ev:
            items = evidence_items(rec)
            if items:
                st.markdown("\n".join(f"- {md_safe(k)}: **{md_safe(v)}**" for k, v in items))
            else:
                st.warning("No evidence attached", icon=":material/report:")   # never hidden
        notes = [(label, rec[key]) for label, key in (("Summary", "summary"), ("Why not the alternative", "why_not"))
                 if isinstance(rec.get(key), str) and rec[key]]
        if notes:
            with st.expander("Summary and why not the alternatives"):
                for label, text in notes:
                    st.markdown(f"**{label}:** {md_safe(text)}")


def render_recommendations(recs, ai_inputs):
    st.markdown("#### Recommendations")
    st.caption(MASKED_NOTE)
    ranked = sort_recommendations(recs)   # measured, simulated, rule-only, unverified, unsafe last
    if not ranked:
        st.info("No recommendations were returned for this query.")
        return
    st.markdown("**Confidence:** " + confidence_strip(confidence_counts(ranked)))
    line = ai_inputs_line(ai_inputs)      # status words only; anything else is left out
    if line:
        st.caption(line)
    for col, text in zip(st.columns(REC_COLUMNS), ["Type", "Fix (masked SQL)", "Reason", "Evidence"]):
        col.caption(text)
    for i, rec in enumerate(ranked):
        render_rec(rec, top=(i == 0 and tier(rec) != "unsafe"))
    if all(tier(r) == "unsafe" for r in ranked):
        st.error("No safe fix was found: every suggested rewrite returned different results.")


# ------------------------------------------------------------------
# One analysis, shown inside an assistant chat message
# ------------------------------------------------------------------
def render_result(r):
    if r.get("match"):
        st.caption(match_line(r["match"]))

    if r.get("leak_check"):
        st.badge("0 raw values exposed", icon=":material/verified_user:", color="green",
                 help="Everything the AI saw or wrote passed the leak check.")
    else:
        st.badge("LEAK DETECTED", icon=":material/gpp_bad:", color="red")
    st.caption(MASKED_NOTE)

    st.markdown("**Masked query** (what the AI sees instead of your real SQL)")
    st.code(r.get("masked_query") or "(none)", language="sql")
    render_bottleneck(r.get("bottleneck"))
    with st.expander("Masked execution plan"):
        st.code(plan_tree_text(r.get("masked_plan"), highlight=bottleneck_node_id(r.get("bottleneck"))),
                language="text")
        st.json(r.get("masked_plan") or {}, expanded=False)
    render_recommendations(r.get("recommendations"), r.get("ai_inputs"))

    # --- TEMPORARY: replaced by 4c (charts) and 4d (Approve). The un-masked deploy_sql is
    # --- deliberately NOT shown here any more: it belongs to the Approve step only.
    if isinstance(r.get("cost_after"), (int, float)) and isinstance(r.get("improvement_pct"), (int, float)):
        st.metric("Planner cost", f"{r['cost_after']:,.0f}", f"-{r['improvement_pct']}%")
    if r.get("explanation"):
        st.markdown(md_safe(r["explanation"]))


# ------------------------------------------------------------------
# Chat
# ------------------------------------------------------------------
if "history" not in st.session_state:
    st.session_state.history = []     # [{"question", "result" or None, "error" or None}]

question = st.chat_input("Ask about a slow report, e.g. 'Why is the weekly sales report slow?' "
                         "or paste a SELECT")
if question:
    entry = {"question": question, "result": None, "error": None}
    with st.spinner("Analysing: masking, finding the bottleneck, simulating fixes..."):
        try:
            entry["result"] = pipeline.analyze(question)
        except ValueError as e:    # rejected SQL, or PrivacyError (a subclass of ValueError)
            entry["error"] = str(e)
    st.session_state.history.append(entry)

with st.sidebar:
    st.toggle("Show sample result (every badge type)", key="show_sample",
              help="Made-up data for checking the layout. Not a real analysis.")

if not st.session_state.history:
    st.info("Ask a question to begin, for example: *Why is the weekly sales report slow?*")

if st.session_state.get("show_sample"):
    with st.chat_message("assistant"):
        st.warning("SAMPLE DATA: made-up numbers that show every badge type. Not a real analysis.",
                   icon=":material/science:")
        render_result(sample_data.demo_result())

for entry in st.session_state.history:
    with st.chat_message("user"):
        st.text(entry["question"])      # the admin's own words: shown only to the admin
    with st.chat_message("assistant"):
        if entry["error"]:
            st.error(entry["error"])
        else:
            render_result(entry["result"])
