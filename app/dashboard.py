"""Streamlit dashboard.  Owner: Person 4.  STATUS: minimal skeleton.

Run:  .venv/Scripts/streamlit run app/dashboard.py
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "import pipeline" works
import pipeline  # noqa: E402

st.title("AI Database Tuner")
question = st.chat_input("Ask about a slow report, e.g. 'Why is the weekly sales report slow?'")
if question:
    r = pipeline.analyze(question)
    st.write(f"**Query:** {r['query_id']}  |  **Privacy:** "
             f"{'0 raw values exposed' if r['leak_check'] else 'LEAK DETECTED'}")
    st.code(r["masked_query"], language="sql")
    st.metric("Planner cost", f"{r['cost_after']:,.0f}", f"-{r['improvement_pct']}%")
    st.write(r["explanation"])
    st.code(r["deploy_sql"], language="sql")
