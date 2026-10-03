"""Demo mode: answers from RECORDED analyses. No database, no sandbox, no Ollama.

The 11 demo questions were analysed once for real (app/record_demo.py) and stored in
app/demo_results.json. In Demo mode a question is matched to one of them by keywords
(pipeline.keyword_match: pure text) and the stored result is returned, with the template
explanation that was stored with it. Nothing is made up: every number is from the real run.

Run the recorder again whenever the AI, the sandbox or the dataset changes:
    .venv/Scripts/python -m app.record_demo
"""
import copy
import json
from pathlib import Path

import pipeline

DEMO_MODE_KEY = "demo_mode"                       # the st.session_state key the sidebar toggle sets
RECORDING = Path(__file__).resolve().parent / "demo_results.json"
LABEL = "DEMO MODE: recorded results from a real run, not a live analysis."


def load():
    """{query_id: result}. Empty if nothing has been recorded yet."""
    try:
        return json.loads(RECORDING.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def answer(question):
    """The stored analysis for `question`, or ValueError with a plain-English reason."""
    if pipeline._looks_like_sql(question):
        raise ValueError("Demo mode answers only the recorded demo questions, not pasted SQL. "
                         "Turn Demo mode off to analyse your own SQL.")
    found = pipeline.keyword_match(question)
    if found is None:
        raise ValueError("Demo mode could not match that to a recorded demo question. "
                         "Try e.g. 'Why is the weekly sales report slow?'")
    q, match = found
    recorded = load()
    if q["id"] not in recorded:
        raise ValueError(f"No recording for '{q['name']}'. Run: python -m app.record_demo")
    result = copy.deepcopy(recorded[q["id"]])
    result["match"] = match                         # the match of THIS question
    result["demo"] = True
    return result
