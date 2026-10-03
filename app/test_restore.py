"""Tests for "restore real values only for the sandbox run and the Approve script".

Run:  .venv/Scripts/python -m app.test_restore      (from the project folder)

(a) a rewrite that contains $1, $2 now really gets verified by the sandbox;
(b) a TRIPWIRE: a real value the admin typed must not appear in anything handed to ai/ or to
    llm.explain, nor in the audit log, nor in the result (except deploy_sql, the Approve script);
(c) the tripwire has teeth: a copy of pipeline.py with the restore moved too early makes it fail.
These use the real masker and the real sandbox (the shared database, read-only runs).
"""
import json
import types
from contextlib import ExitStack
from pathlib import Path

import sqlglot

import pipeline
from ai import gnn, rl
from app import llm
from app.test_pipeline_hooks import FakeRL, patched
from app.test_privacy import fresh_audit_log
from db import sandbox
from privacy import mask


def _no_ollama(prompt):
    raise llm.LLMUnavailable("Ollama is off in this test")


def masked_like_the_pipeline(sql):
    """What the real masker would write for `sql` (a fresh mapping, same key -> same codes)."""
    return mask.mask_query(sql, mask.build_mapping())


# ---------------------------------------------------------------- the restorer on its own
def test_restorer_rebuilds_the_query_and_keeps_hostile_values_inside_their_quotes():
    sql = ("SELECT count(*) FROM customers WHERE name = 'Rahul Sharma' AND customer_id = 90210 "
           "AND email = 'o''brien@x.com' AND segment = 'x''; DROP TABLE orders; --'")
    m = mask.build_mapping()
    masked = mask.mask_query(sql, m)
    assert "Rahul" not in masked and "90210" not in masked and "$1" in masked
    back = pipeline._restorer(sql, m)(mask.unmask(masked, m))
    assert sqlglot.parse_one(back, read="postgres") == sqlglot.parse_one(sql, read="postgres")
    assert len(sqlglot.parse(back, read="postgres")) == 1                # still ONE statement
    assert "customer_id = 90210" in back                                  # a number stays bare
    assert pipeline._restorer(sql, m)("SELECT $9") == "SELECT $9"         # a placeholder it never made is left alone
    assert pipeline._restorer(sql, mask.build_mapping())("SELECT $1") == "SELECT $1"   # no record -> nothing restored


# ---------------------------------------------------------------- (a) a rewrite with $1, $2 gets verified
ORIG = "SELECT count(*) FROM orders WHERE customer_id = 34950 AND status = 'delivered'"
EQUAL = "SELECT count(*) FROM orders o WHERE o.customer_id = 34950 AND o.status = 'delivered'"
DIFFERENT = "SELECT count(*) FROM orders WHERE customer_id = 34950 OR status = 'delivered'"


def test_a_rewrite_with_placeholders_is_now_verified_by_the_sandbox():
    equal_m, different_m = masked_like_the_pipeline(EQUAL), masked_like_the_pipeline(DIFFERENT)
    assert "$1" in equal_m and "$2" in different_m, (equal_m, different_m)
    fake = FakeRL(rewrites=[equal_m, different_m])
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (rl, "recommend", fake)]:
            stack.enter_context(patched(obj, name, value))
        r = pipeline.analyze(ORIG)                                         # the real sandbox runs everything
        events = [e["event"] for e in llm.get_audit_log() if e["event"] != "llm_call"]
    answers = fake.time_answers
    print(f"      P3 received results_match: original={answers[0]['results_match']}, "
          f"equal rewrite={answers[1]['results_match']}, different rewrite={answers[2]['results_match']}")
    assert all(set(a) == {"ms", "results_match"} for a in answers)
    assert [a["results_match"] for a in answers] == [True, True, False]
    assert r["ai_inputs"]["time_fn"] == "used" and "time_fn_failed" not in events
    assert r["unsafe_rewrite"] is True
    flagged = {x["action"]: x.get("unsafe_rewrite", False) for x in r["recommendations"] if x["kind"] == "rewrite"}
    assert flagged == {equal_m: False, different_m: True}


# ---------------------------------------------------------------- (b) the tripwire
SENSITIVE = ["Rahul Sharma", "zz_marker_77@example.com", "90210"]
SQL_B = ("SELECT count(*) FROM customers WHERE name = 'Rahul Sharma' "
         "AND email = 'zz_marker_77@example.com' AND customer_id = 90210")
REWRITE_B = ("SELECT count(*) FROM customers c WHERE c.name = 'Rahul Sharma' "
             "AND c.email = 'zz_marker_77@example.com' AND c.customer_id = 90210")


def real_values_in(text):
    return [v for v in SENSITIVE if v.lower() in text.lower()]


def run_tripwire(module):
    """Run `module.analyze` on a query full of real values and report where they showed up.
    Returns (handed to the AI side, sandbox saw them, result without the script, audit log, deploy_sql)."""
    rewrite_m = masked_like_the_pipeline(REWRITE_B)
    to_ai, sandbox_saw = [], []
    real_run, real_cost = sandbox.checked_run, sandbox.cost

    def spying_run(sql, index_sqls=[]):
        sandbox_saw.append(sql)
        return real_run(sql, index_sqls)

    def spying_cost(sql, hypo=[]):
        sandbox_saw.append(sql)
        return real_cost(sql, hypo)

    real_bottleneck, real_explain = gnn.find_bottleneck, llm.explain

    def recommend(masked_sql, masked_plan, cost_fn, stats=None, time_fn=None, agent=None, column_types=None):
        to_ai.append(json.dumps([masked_sql, masked_plan, stats, column_types], default=str))
        time_fn(masked_sql)
        time_fn(rewrite_m)
        cost_fn(rewrite_m, [])
        return [{"kind": "rewrite", "action": rewrite_m, "cost_before": 100.0, "cost_after": 50.0,
                 "reason": "Same result, simpler.", "verified_by": "timed_run"}]

    def bottleneck(masked_plan):
        to_ai.append(json.dumps(masked_plan, default=str))
        return real_bottleneck(masked_plan)

    def explain(masked_result, leak_check):
        to_ai.append(json.dumps(masked_result, default=str))          # what llm.explain is HANDED
        return real_explain(masked_result, leak_check)

    def call_ollama(prompt):
        to_ai.append(prompt)
        return "Canned explanation.", {}

    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "start_warmup", lambda: None), (llm, "_call_ollama", call_ollama),
                                 (llm, "explain", explain), (rl, "recommend", recommend),
                                 (gnn, "find_bottleneck", bottleneck),
                                 (sandbox, "checked_run", spying_run), (sandbox, "cost", spying_cost)]:
            stack.enter_context(patched(obj, name, value))
        r = module.analyze(SQL_B)
        audit = json.dumps(llm.get_audit_log(), default=str)
    # deploy_sql / deploy_scripts ARE the Approve text, the one place real values may live
    result_without_script = json.dumps({k: v for k, v in r.items() if k not in ("deploy_sql", "deploy_scripts")},
                                       default=str)
    return to_ai, sandbox_saw, result_without_script, audit, r["deploy_sql"]


def violations(module):
    to_ai, _, result, audit, _ = run_tripwire(module)
    found = {}
    for place, text in (("handed to ai/ or llm.explain", " ".join(to_ai)), ("the result", result),
                        ("the audit log", audit)):
        if real_values_in(text):
            found[place] = real_values_in(text)
    return found


def test_tripwire_no_real_value_reaches_ai_the_llm_the_log_or_the_result():
    to_ai, sandbox_saw, result, audit, deploy = run_tripwire(pipeline)
    print(f"      things handed to ai/ + llm.explain: {len(to_ai)}; sandbox calls: {len(sandbox_saw)}")
    assert to_ai, "the tripwire saw nothing: the capture is broken"
    assert violations(pipeline) == {}
    # the values DID reach the two places that may have them:
    assert any(all(v in s for v in SENSITIVE) and "$" not in s for s in sandbox_saw), "sandbox never got the real values"
    assert all(v in deploy for v in SENSITIVE), "the Approve script is missing the real values"
    assert "$1" not in deploy


# ---------------------------------------------------------------- (c) the tripwire has teeth
def load_pipeline(*replacements):
    """A COPY of pipeline.py with deliberate bugs, loaded as its own module."""
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in source, old[:60]
        source = source.replace(old, new, 1)
    module = types.ModuleType("pipeline_with_a_bug")
    exec(compile(source, "pipeline_with_a_bug", "exec"), module.__dict__)
    return module


RESTORE_BEFORE_THE_LLM = (
    "    llm_out = llm.explain(masked_result, lambda text: is_clean(text, free_text=True))",
    "    for _rec in masked_result['recommendations']:      # BUG: restore moved before the LLM call\n"
    "        _rec['action'] = restore(mask.unmask(_rec.get('action') or '', mapping))\n"
    "    llm_out = llm.explain(masked_result, lambda text: is_clean(text, free_text=True))")
RESTORE_BEFORE_THE_AI = (
    "        recs = rl.recommend(masked_sql, masked_plan, cost_fn, **extra)",
    "        recs = rl.recommend(restore(masked_sql), masked_plan, cost_fn, **extra)   # BUG: restore before the AI call")
NO_RESTORE_AT_ALL = (
    '    restore = _restorer(q["sql"], mapping)',
    '    restore = lambda sql: sql   # BUG: no restore')


def test_tripwire_goes_red_if_the_restore_moves_before_the_llm_call():
    found = violations(load_pipeline(RESTORE_BEFORE_THE_LLM))
    print(f"      with the bug: {found}")
    assert "handed to ai/ or llm.explain" in found


def test_tripwire_goes_red_if_the_restore_moves_before_the_ai_call():
    found = violations(load_pipeline(RESTORE_BEFORE_THE_AI))
    print(f"      with the bug: {found}")
    assert "handed to ai/ or llm.explain" in found


def test_without_the_restore_rewrites_fail_to_verify_again():
    """The old behaviour: no restore -> $1 reaches the sandbox -> the timing run fails."""
    broken = load_pipeline(NO_RESTORE_AT_ALL)
    fake = FakeRL(rewrites=[masked_like_the_pipeline(EQUAL)])
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (rl, "recommend", fake)]:
            stack.enter_context(patched(obj, name, value))
        r = broken.analyze(ORIG)
    assert r["ai_inputs"]["time_fn"] == "failed: rewrites are unverified"


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
