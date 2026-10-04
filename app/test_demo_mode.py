"""Tests for Demo mode (step A).

Run:  .venv/Scripts/python -m app.test_demo_mode      (from the project folder)

With Demo mode on, a question must be answered from the RECORDED analyses and touch nothing live:
here every sandbox function, psycopg.connect, pipeline.analyze, the Ollama call and the Ollama
warm-up are rigged to EXPLODE if they are called. The sample-data switch is hidden.
"""
from contextlib import ExitStack, contextmanager

from streamlit.testing.v1 import AppTest

import pipeline
from app import demo_mode, llm
from app import test_recommendations as tr
from app.test_approve import approve_buttons, explode, rig_targets, script_on
from app.test_privacy import fresh_audit_log, patched
from app.test_recommendations import page_text, sample_shown
from db import queries

WEEKLY = "Why is the weekly sales report slow?"


def boom(*a, **k):
    raise AssertionError("Demo mode touched something live")


@contextmanager
def demo_page(demo=True, warmups=None):
    """The dashboard with demo mode preset. Everything live explodes while the page is open."""
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        warm = (lambda: warmups.append(1)) if warmups is not None else boom
        live = [(llm, "start_warmup", warm), (llm, "_call_ollama", boom), (llm, "_post", boom),
                (pipeline, "analyze", boom), *rig_targets()]
        for obj, name, value in live:
            stack.enter_context(patched(obj, name, value))
        at = AppTest.from_file(tr.DASHBOARD, default_timeout=30)
        at.session_state[demo_mode.DEMO_MODE_KEY] = demo
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        yield at


def ask(at, question):
    at.chat_input[0].set_value(question).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


# ---------------------------------------------------------------- the recording
def test_the_recording_has_every_demo_query_and_is_clean():
    rec = demo_mode.load()
    assert sorted(rec) == sorted(q["id"] for q in queries.QUERIES), sorted(rec)
    for qid, r in rec.items():
        assert r["leak_check"] is True and r["llm"]["source"] == "template", qid
        assert r["recommendations"] and r["explanation"], qid


# ---------------------------------------------------------------- the page
def test_demo_mode_answers_from_the_recording_and_touches_nothing_live():
    with demo_page() as at:                       # sandbox, DB, analyze, Ollama: all explode
        ask(at, WEEKLY)
        text = page_text(at)
        assert demo_mode.LABEL in [i.value for i in at.info] and "Demo mode" in text
        assert "Weekly sales report by region" in text and "Recommendations" in text
        assert "Explanation" in text and "SAMPLE DATA" not in text and not sample_shown(at)
        assert at.exception == []


def test_the_sample_switch_is_hidden_and_the_demo_switch_is_there():
    with demo_page() as at:
        keys = [t.key for t in at.sidebar.toggle]
        assert keys == [demo_mode.DEMO_MODE_KEY], keys
    with demo_page(demo=False, warmups=[]) as at:
        assert [t.key for t in at.sidebar.toggle] == [demo_mode.DEMO_MODE_KEY, "show_sample"]


def test_every_recorded_question_gets_its_own_recording_in_demo_mode():
    with demo_page() as at:
        for q in queries.QUERIES:
            ask(at, q["name"])
        shown = [i.value for i in at.info if i.value == demo_mode.LABEL]
        assert len(shown) == len(queries.QUERIES)
        text = page_text(at)
        for q in queries.QUERIES:                       # each question got ITS OWN query's recording
            assert f"**{q['name']}** ({q['id']})" in text, q["id"]


def test_pasted_sql_and_unknown_questions_get_a_plain_message_and_touch_nothing():
    with demo_page() as at:
        ask(at, "SELECT * FROM customers WHERE name = 'Rahul Sharma'")
        ask(at, "what is the weather on mars")
        errors = [e.value for e in at.error]
        assert any("pasted SQL" in e for e in errors) and any("could not match" in e for e in errors), errors
        assert "Rahul" not in " ".join(errors).replace("'Rahul Sharma'", "")


def test_demo_mode_never_starts_the_ollama_warmup_but_normal_mode_does():
    with demo_page(demo=True) as at:               # start_warmup = boom: would raise
        assert not at.exception
    calls = []
    with demo_page(demo=False, warmups=calls):
        pass
    assert calls, "normal mode should still warm the model up"


def test_switching_demo_mode_on_mid_session_sends_the_next_question_to_the_recording():
    with demo_page(demo=False, warmups=[]) as at:
        at.session_state[demo_mode.DEMO_MODE_KEY] = True
        at.run()
        ask(at, WEEKLY)                            # pipeline.analyze = boom, so it must use the recording
        assert demo_mode.LABEL in [i.value for i in at.info]


def test_approve_still_works_in_demo_mode_and_runs_nothing():
    with demo_page() as at:
        ask(at, WEEKLY)
        approve_buttons(at)[0].click().run()
        assert not at.exception and script_on(at) is not None and "BEGIN;" in script_on(at)


def run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            print(f"RUN   {name}")
            fn()
            print(f"PASS  {name}")
        except Exception as e:                       # noqa: BLE001 - report every failure
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:300]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
