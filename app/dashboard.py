"""Streamlit dashboard.  Owner: Person 4.  STATUS: Step 4a (chat, status and privacy
badges, match line, masked query and plan).  4b-4d add recommendations, charts and
the Approve script.

Run:  .venv/Scripts/streamlit run app/dashboard.py
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "import pipeline" works
import pipeline  # noqa: E402
from app import llm  # noqa: E402
from app.ui_helpers import match_line, plan_tree_text  # noqa: E402

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


# ------------------------------------------------------------------
# One analysis, shown inside an assistant chat message
# ------------------------------------------------------------------
def render_result(r):
    if r.get("match"):
        st.caption(match_line(r["match"]))

    if r["leak_check"]:
        st.badge("0 raw values exposed", icon=":material/verified_user:", color="green",
                 help="Everything the AI saw or wrote passed the leak check.")
    else:
        st.badge("LEAK DETECTED", icon=":material/gpp_bad:", color="red")

    st.markdown("**Masked query** (what the AI sees instead of your real SQL)")
    st.code(r["masked_query"], language="sql")
    with st.expander("Masked execution plan"):
        st.code(plan_tree_text(r["masked_plan"]), language="text")
        st.json(r["masked_plan"], expanded=False)

    # --- TEMPORARY: replaced by 4b (recommendations), 4c (charts), 4d (Approve) ---
    st.metric("Planner cost", f"{r['cost_after']:,.0f}", f"-{r['improvement_pct']}%")
    st.write(r["explanation"])
    st.code(r["deploy_sql"], language="sql")


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

if not st.session_state.history:
    st.info("Ask a question to begin, for example: *Why is the weekly sales report slow?*")

for entry in st.session_state.history:
    with st.chat_message("user"):
        st.text(entry["question"])      # the admin's own words: shown only to the admin
    with st.chat_message("assistant"):
        if entry["error"]:
            st.error(entry["error"])
        else:
            render_result(entry["result"])
