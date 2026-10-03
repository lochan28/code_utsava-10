"""Plain-English explanation from the local LLM (Ollama, qwen2.5:7b).

Owner: Person 4.  STATUS: STUB - no LLM call yet.  Receives ONLY masked data.
Self-test:  .venv/Scripts/python -m app.llm
"""

# ============================================================
# TUNABLE SETTINGS
# ============================================================
MODEL = "qwen2.5:7b"
OLLAMA_URL = "http://localhost:11434/api/generate"


def explain(masked_result):
    r = masked_result["recommendations"][0]
    return (f"The query is slow because: {masked_result['bottleneck']['reason']} "
            f"Suggested fix: {r['action']} ({r['reason']})")


if __name__ == "__main__":
    print(explain({"bottleneck": {"reason": "Full scan."},
                   "recommendations": [{"action": "CREATE INDEX ...", "reason": "no index"}]}))
