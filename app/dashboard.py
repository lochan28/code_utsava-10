"""Streamlit dashboard.  Owner: Person 4.  STATUS: minimal skeleton.

Run:  .venv/Scripts/streamlit run app/dashboard.py
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "import pipeline" works
import pipeline  # noqa: E402
from app import llm  # noqa: E402

llm.start_warmup()   # load the model in the background at app start; only the first call acts

st.title("AI Database Tuner")
_s = llm.llm_status()   # the full status badge comes in Step 4; this keeps it visible meanwhile
st.caption(f"LLM: **{_s['status']}** · {_s['model']} · {_s['reason']}")
question = st.chat_input("Ask about a slow report, e.g. 'Why is the weekly sales report slow?'")
if question:
    try:
        r = pipeline.analyze(question)
    except ValueError as e:          # e.g. pasted SQL that is not a single read-only SELECT
        st.error(str(e))
        st.stop()
    m = r.get("match")
    if m:
        st.caption(f"Matched **{m['name']}** ({m['query_id']}), {m['confidence']:.0%} sure "
                   f"· method: {m['method']}"
                   + (f" · runner-up: {m['runner_up']}" if m["runner_up"] else ""))
    st.write(f"**Query:** {r['query_id']}  |  **Privacy:** "
             f"{'0 raw values exposed' if r['leak_check'] else 'LEAK DETECTED'}")
    st.code(r["masked_query"], language="sql")
    st.metric("Planner cost", f"{r['cost_after']:,.0f}", f"-{r['improvement_pct']}%")
    st.write(r["explanation"])
    st.code(r["deploy_sql"], language="sql")
