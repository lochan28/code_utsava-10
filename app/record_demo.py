"""Record the demo analyses (needs the real database; Ollama is NOT used).

Run (from the project folder, Docker database up):
    .venv/Scripts/python -m app.record_demo
Writes app/demo_results.json: one real analysis per demo query, with the template explanation.
"""
import json

import pipeline
from app import llm
from app.demo_mode import RECORDING
from db import queries


def _no_ollama(prompt):
    raise llm.LLMUnavailable("recording uses the template explanation")


def record():
    llm._call_ollama = _no_ollama
    out = {}
    for q in queries.QUERIES:
        r = pipeline.analyze(q["name"])
        assert r["match"]["query_id"] == q["id"], (q["id"], r["match"])
        out[q["id"]] = r
        print(f"{q['id']}  {q['name']}  leak_check={r['leak_check']}")
    RECORDING.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"wrote {RECORDING} ({len(out)} analyses)")


if __name__ == "__main__":
    record()
