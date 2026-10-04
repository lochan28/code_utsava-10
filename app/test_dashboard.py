"""Tests for the dashboard (Step 4a) and its helpers, using Streamlit's headless AppTest.

Run:  .venv/Scripts/python -m app.test_dashboard      (from the project folder)

Ollama is switched off in these tests (its call is replaced), so they give the same
result whether or not Ollama is running on the machine.
"""
from contextlib import ExitStack
from pathlib import Path

from streamlit.testing.v1 import AppTest

import pipeline
from app import llm
from app.test_support import AI_STAND_INS, T
from app.test_privacy import PASTED, fresh_audit_log, patched
from app.ui_helpers import match_line, plan_tree_text
from privacy import mask

DASHBOARD = str(Path(__file__).with_name("dashboard.py"))
QUESTION = "Why is the weekly sales report slow?"


def _no_ollama(prompt):
    raise llm.LLMUnavailable("Ollama is off in this test")


def run_app(*questions):
    """Open the dashboard, ask each question in turn, return the AppTest."""
    at = AppTest.from_file(DASHBOARD, default_timeout=30).run()
    for q in questions:
        at.chat_input[0].set_value(q).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def markdown_values(at):
    return [m.value for m in at.markdown]


# ---------------------------------------------------------------- helpers
def test_plan_tree_text_is_an_indented_tree():
    plan = {"Plan": {"Node Type": "Hash Join", "Total Cost": 20000.0, "Plan Rows": 900, "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "t_a3f", "Total Cost": 19437.0, "Plan Rows": 1200},
        {"Node Type": "Hash", "Total Cost": 50.0, "Plan Rows": 10, "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "t_b22", "Total Cost": 20.0, "Plan Rows": 10}]}]}}
    assert plan_tree_text(plan).splitlines() == [
        "Hash Join  (cost 20,000, ~900 rows)",
        "   -> Seq Scan on t_a3f  (cost 19,437, ~1,200 rows)",
        "   -> Hash  (cost 50, ~10 rows)",
        "      -> Seq Scan on t_b22  (cost 20, ~10 rows)"]
    assert plan_tree_text([plan]) == plan_tree_text(plan)            # list-wrapped JSON plan
    assert plan_tree_text({}) == plan_tree_text(None) == plan_tree_text([]) == "(no plan available)"
    assert "?" in plan_tree_text({"Plan": {"Node Type": "Seq Scan"}})   # missing numbers are not a crash


def test_match_line_text():
    m = {"name": "Weekly sales", "query_id": "q11", "confidence": 0.824, "method": "keywords", "runner_up": "q02"}
    assert match_line(m) == "Matched **Weekly sales** (q11), 82% sure · keywords · runner-up: q02"
    assert "runner-up" not in match_line({**m, "runner_up": None})
    assert match_line(None) == ""


# ---------------------------------------------------------------- the page
def test_first_render_shows_hint_and_status_badge():
    with fresh_audit_log(), patched(llm, "_call_ollama", _no_ollama), patched(llm, "start_warmup", lambda: None):
        llm._reset_state()
        at = run_app()
        assert any("Ask a question to begin" in i.value for i in at.info)
        assert any(v.startswith(":gray-badge[") and "template fallback" in v for v in markdown_values(at))
        assert not at.chat_message


def test_question_shows_match_privacy_badge_masked_query_and_plan():
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 *AI_STAND_INS]:
            stack.enter_context(patched(obj, name, value))
        at = run_app(QUESTION)
        md = markdown_values(at)
        print(f"      captions: {[c.value[:60] for c in at.caption][-1:]}")
        assert [m.name for m in at.chat_message] == ["user", "assistant"]
        assert any(c.value.startswith("Matched **Weekly sales report by region** (q11)") for c in at.caption)
        assert any(v.startswith(":green-badge[") and "0 raw values exposed" in v for v in md)
        assert any("Masked query" in v for v in md)
        assert any(c.value.startswith("SELECT") and T in c.value for c in at.code)       # masked query (real masked name)
        assert [e.label for e in at.expander] == ["Masked execution plan",
                                                  "Planner estimate (can disagree with measured time)"]
        assert any(f"Seq Scan on {T}" in c.value for c in at.code)                       # plan tree
        # the real table name must not appear anywhere in the masked parts of the page
        masked_parts = [c.value for c in at.code if c.language in ("sql", "text")][:2]
        assert not any("orders" in p for p in masked_parts), masked_parts


def test_unsafe_sql_shows_an_error_and_history_keeps_both_questions():
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 *AI_STAND_INS]:      # no real sandbox runs: they share a lock with the team
            stack.enter_context(patched(obj, name, value))
        at = run_app(QUESTION, "select 1; drop table orders")
        assert [m.name for m in at.chat_message] == ["user", "assistant", "user", "assistant"]
        assert [e.value for e in at.error] == ["Paste exactly one SQL statement."]
        assert any("0 raw values exposed" in v for v in markdown_values(at)), "first answer disappeared"
        at.chat_input[0].set_value("customer order history is slow").run()
        assert len(at.chat_message) == 6 and not at.exception


def test_blocked_analysis_shows_the_admin_friendly_message():
    with fresh_audit_log(), patched(llm, "_call_ollama", _no_ollama), patched(llm, "start_warmup", lambda: None), \
            patched(mask, "mask_query", lambda sql, m: sql):
        at = run_app(PASTED)
        assert [e.value for e in at.error] == [pipeline.BLOCKED_MESSAGE]
        assert "Rahul" in at.chat_message[0].text[0].value      # the admin's own words are shown to the admin
        assert not any("Rahul" in v for v in markdown_values(at))   # ...and nowhere else


def test_red_badge_when_the_leak_check_fails():
    real = pipeline.analyze
    with fresh_audit_log(), patched(llm, "_call_ollama", _no_ollama), patched(llm, "start_warmup", lambda: None):
        bad = real(QUESTION)
    bad["leak_check"] = False
    with patched(pipeline, "analyze", lambda q: bad), patched(llm, "start_warmup", lambda: None):
        at = run_app(QUESTION)
        md = markdown_values(at)
        assert any(v.startswith(":red-badge[") and "LEAK DETECTED" in v for v in md)
        assert not any("0 raw values exposed" in v for v in md)


def test_status_badge_colours_and_reason():
    cases = {"live LLM": ":green-badge[", "warming up": ":orange-badge[", "template fallback": ":gray-badge["}
    for status, prefix in cases.items():
        fake = lambda s=status: {"status": s, "reason": f"reason for {s}", "model": "qwen2.5:7b"}
        with patched(llm, "llm_status", fake), patched(llm, "start_warmup", lambda: None):
            at = run_app()
            assert any(v.startswith(prefix) and status in v for v in markdown_values(at)), status
            shown_reason = any(c.value == f"reason for {status}" for c in at.caption)
            assert shown_reason == (status != "live LLM"), status


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            print(f"RUN   {name}")
            fn()
            print(f"PASS  {name}")
        except Exception as e:                       # noqa: BLE001 - report every failure
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
