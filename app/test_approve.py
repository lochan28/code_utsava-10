"""Tests for Step 4d: the Approve step.

Run:  .venv/Scripts/python -m app.test_approve      (from the project folder)

What must hold:
- Approve only SHOWS a script. It never executes anything: here the sandbox, every database
  connection and the decoder are rigged to EXPLODE if the page touches them.
- the un-masked script appears only AFTER the click, and only inside the script itself;
- it is the script of the TOP PICK (not just the first fix listed), never of an unsafe fix;
- the script says NOT EXECUTED and is wrapped in BEGIN ... ROLLBACK.
"""
import copy
import inspect
from contextlib import ExitStack, contextmanager

import psycopg
from streamlit.testing.v1 import AppTest

import pipeline
from app import approve, llm, sample_data
from app import test_recommendations as tr
from app.test_privacy import fresh_audit_log, patched
from app.test_recommendations import _decoder_must_not_run, _no_ollama, full, page_text
from app.test_support import AI_STAND_INS, C_REGION, T
from db import sandbox
from privacy import mask

REWRITE_SQL = "SELECT count(*) FROM customers WHERE name = 'Rahul Sharma' AND customer_id = 90210"
INDEX_SQL = "CREATE INDEX ON orders (region_id, order_date)"
UNSAFE_SQL = "SELECT 1 -- unsafe-marker"
QUESTION = "why is Rahul Sharma's page slow"


def explode(*args, **kwargs):
    raise AssertionError("the Approve step touched the database")


def rig_targets():
    """Every function defined in db/sandbox.py, plus psycopg.connect: all explode if called."""
    fns = [(sandbox, name, explode) for name, f in inspect.getmembers(sandbox, inspect.isfunction)
           if f.__module__ == sandbox.__name__]
    return fns + [(psycopg, "connect", explode)]


def approve_result(top_first=True):
    """A finished analysis: a measured rewrite (the top pick), a simulated index, an unsafe rewrite."""
    s = sample_data.SAMPLE_RECOMMENDATIONS
    rewrite, index, unsafe = (copy.deepcopy(s[4]), copy.deepcopy(s[3]), copy.deepcopy(s[0]))
    recs = [rewrite, index, unsafe] if top_first else [index, rewrite, unsafe]
    script_of = {id(rewrite): REWRITE_SQL, id(index): INDEX_SQL, id(unsafe): UNSAFE_SQL}
    r = full(recommendations=recs)
    r["deploy_scripts"] = [script_of[id(x)] for x in recs]
    r["deploy_sql"] = r["deploy_scripts"][0]
    return r


@contextmanager
def page_with(result, question=QUESTION):
    """The dashboard, one question asked, analyze() returning `result`. Sandbox, connections and
    decoder are rigged to explode for the whole time the page is open (including the clicks)."""
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (pipeline, "analyze", lambda q: result), (mask, "unmask", _decoder_must_not_run),
                                 *rig_targets()]:
            stack.enter_context(patched(obj, name, value))
        at = AppTest.from_file(tr.DASHBOARD, default_timeout=30).run()
        at.chat_input[0].set_value(question).run()
        assert not at.exception, [e.value for e in at.exception]
        yield at


def script_on(at):
    shown = [c.value for c in at.code if "NOT EXECUTED" in c.value]
    return shown[0] if shown else None


def approve_buttons(at):
    return [b for b in at.button if b.label == "Approve and show the script"]


# ---------------------------------------------------------------- the script text
def test_script_is_wrapped_and_says_not_executed():
    for kind, sql in (("index", INDEX_SQL), ("rewrite", REWRITE_SQL), ("partition", "PARTITION orders BY RANGE (order_date) MONTHLY"),
                      ("none", ""), (None, "")):
        lines = approve.build_script(kind, sql, "measured").splitlines()
        assert lines[1] == "-- NOT EXECUTED. Review before running." and "BEGIN;" in lines
        assert lines.index("BEGIN;") < len(lines) - 1 and lines[-1].startswith("ROLLBACK;") and "COMMIT" in lines[-1]
        between = lines[lines.index("BEGIN;") + 1:-1]
        assert between, kind


def test_script_bodies_per_kind():
    assert "CREATE INDEX ON orders (region_id, order_date);" in approve.build_script("index", INDEX_SQL).splitlines()
    two = approve.build_script("index", "CREATE INDEX ON orders (a); CREATE INDEX ON orders (b)")
    assert "CREATE INDEX ON orders (a);" in two and "CREATE INDEX ON orders (b);" in two
    rw = approve.build_script("rewrite", REWRITE_SQL + ";")
    assert "REPLACEMENT QUERY" in rw and rw.count(";\n") >= 2 and REWRITE_SQL + ";" in rw
    part = approve.build_script("partition", "PARTITION orders BY RANGE (order_date) MONTHLY")
    body = part.splitlines()[part.splitlines().index("BEGIN;") + 1:-1]
    assert all(line.startswith("--") for line in body), "a partition script must contain NO executable statement"
    # an "index" that is really something else is never turned into a statement
    assert "DROP TABLE" not in approve.build_script("index", "DROP TABLE orders").replace("-- ", "")
    assert "No executable change" in approve.build_script("index", "DROP TABLE orders")


def test_sql_for_picks_the_script_that_belongs_to_the_fix():
    r = approve_result(top_first=False)                  # order: index, rewrite, unsafe
    recs = r["recommendations"]
    assert approve.sql_for(r, recs[1]) == REWRITE_SQL and approve.sql_for(r, recs[0]) == INDEX_SQL
    assert approve.sql_for(r, {"kind": "stranger"}) is None
    old = {"recommendations": recs, "deploy_sql": "FIRST ONLY"}              # an older result: only the first fix has a script
    assert approve.sql_for(old, recs[0]) == "FIRST ONLY" and approve.sql_for(old, recs[1]) is None
    assert approve.sql_for({}, recs[0]) is None and approve.sql_for({"recommendations": "x"}, recs[0]) is None


# ---------------------------------------------------------------- the page
def test_nothing_unmasked_is_on_the_page_before_the_click():
    with page_with(approve_result()) as at:
        text = page_text(at)
        assert "NOT EXECUTED" not in text and "BEGIN;" not in text and "ROLLBACK" not in text
        assert "customer_id = 90210" not in text and "CREATE INDEX ON orders" not in text
        assert "rahul" not in text.lower() and "90210" not in text
        assert [b.label for b in approve_buttons(at)] == ["Approve and show the script"]
        assert any(c.value.startswith("Approving only SHOWS the script") for c in at.caption)
        assert script_on(at) is None and not at.get("download_button")


def test_clicking_approve_shows_the_script_and_runs_nothing():
    with page_with(approve_result()) as at:                 # sandbox + connections + decoder rigged to explode
        approve_buttons(at)[0].click().run()
        assert not at.exception, [e.value for e in at.exception]
        script = script_on(at)
        print(f"      script shown ({len(script.splitlines())} lines), first lines: {script.splitlines()[:2]}")
        assert script is not None and "BEGIN;" in script and "ROLLBACK;" in script
        assert REWRITE_SQL + ";" in script                                  # the top pick (a measured rewrite)
        assert INDEX_SQL not in script and "unsafe-marker" not in script    # not the other fixes
        assert any(w.value.startswith("NOT EXECUTED, review before running.") for w in at.warning)
        assert len(at.get("download_button")) == 1


def test_the_script_is_the_top_picks_even_when_it_is_not_the_first_fix_listed():
    with page_with(approve_result(top_first=False)) as at:
        approve_buttons(at)[0].click().run()
        script = script_on(at)
        assert REWRITE_SQL + ";" in script and INDEX_SQL not in script


def test_real_values_appear_only_inside_the_script():
    with page_with(approve_result()) as at:
        before = page_text(at).lower()
        assert "rahul" not in before and "zz" not in before                    # (the admin's own bubble is skipped)
        approve_buttons(at)[0].click().run()
        script = script_on(at)
        rest = page_text(at).replace(script, "")                                # the page WITHOUT the script
        assert "Rahul" in script and "90210" in script
        assert "rahul" not in rest.lower() and "90210" not in rest, "a real value leaked outside the script"


def test_approval_survives_reruns_and_each_analysis_needs_its_own_click():
    result = approve_result()
    with page_with(result) as at:
        approve_buttons(at)[0].click().run()
        assert script_on(at) is not None
        at.run()                                                                # any other rerun
        assert script_on(at) is not None
        at.chat_input[0].set_value("another question").run()                    # a second analysis
        assert len(approve_buttons(at)) == 2
        assert len([c for c in at.code if "NOT EXECUTED" in c.value]) == 1      # only the first is approved


def test_no_approve_button_when_no_fix_is_safe():
    unsafe_only = [x for x in sample_data.SAMPLE_RECOMMENDATIONS if x.get("unsafe_rewrite")]
    r = full(recommendations=copy.deepcopy(unsafe_only))
    r["deploy_scripts"] = [UNSAFE_SQL]
    with page_with(r) as at:
        assert approve_buttons(at) == [] and script_on(at) is None
        assert any(c.value == "No safe fix to approve." for c in at.caption)
        assert "unsafe-marker" not in page_text(at)


def test_the_sample_result_has_no_approve_step():
    at, patches = tr.open_page()
    with patches:
        at.sidebar.toggle[0].set_value(True).run()
        assert not at.exception and approve_buttons(at) == [] and script_on(at) is None


def test_the_pipeline_keeps_one_unmasked_script_per_recommendation():
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None), *AI_STAND_INS]:
            stack.enter_context(patched(obj, name, value))
        r = pipeline.analyze("Why is the weekly sales report slow?")
    assert len(r["deploy_scripts"]) == len(r["recommendations"]) == 1
    assert r["deploy_scripts"][0] == r["deploy_sql"] == "CREATE INDEX ON orders (region_id)"
    assert T not in r["deploy_sql"] and C_REGION not in r["deploy_sql"]          # masked codes are gone


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
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:200]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
