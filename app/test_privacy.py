"""Privacy and safety tests for pipeline.py and app/llm.py.

Run:  .venv/Scripts/python -m app.test_privacy      (from the project folder)
Plain asserts, no pytest needed (pytest can also run it).

Where a test needs an LLM reply and Ollama is not wired up yet, a FAKE LLM is
swapped in to exercise the checks. Those tests say "fake LLM" in their name.
"""
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pipeline
from ai import rl
from app import llm
from db import queries
from privacy import mask


@contextmanager
def fresh_audit_log():
    """Point the audit log at a temp file for one test, then restore it."""
    old_path, old_log = llm.AUDIT_LOG_PATH, list(llm.AUDIT_LOG)
    llm.AUDIT_LOG_PATH = Path(tempfile.mkdtemp()) / "audit_log.jsonl"
    llm.AUDIT_LOG.clear()
    try:
        yield llm.AUDIT_LOG_PATH
    finally:
        llm.AUDIT_LOG_PATH = old_path
        llm.AUDIT_LOG[:] = old_log


@contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def everything_logged(path):
    """All text the audit log holds, in memory and on disk, lower-cased."""
    return (json.dumps(llm.get_audit_log()) + path.read_text(encoding="utf-8")).lower()


# ---------------------------------------------------------------- pasted SQL guard
def test_guard_accepts_all_demo_queries():
    for q in queries.QUERIES:
        pipeline._read_only_sql(q["sql"])


def test_guard_rejects_dangerous_sql():
    bad = {
        "two statements": "select 1; drop table orders",
        "delete inside WITH": "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
        "SELECT INTO (creates a table)": "SELECT * INTO newtab FROM orders",
        "FOR UPDATE (locks rows)": "SELECT * FROM orders FOR UPDATE",
        "pg_sleep": "SELECT pg_sleep(100)",
        "pg_read_file": "SELECT pg_read_file('/etc/passwd')",
        "nextval": "SELECT nextval('s')",
        "schema-qualified function": "SELECT pg_catalog.pg_sleep(5)",
        "does not parse": "SELECT * FROM",
    }
    for why, sql in bad.items():
        try:
            pipeline._read_only_sql(sql)
        except ValueError:
            continue
        raise AssertionError(f"accepted: {why}")


# ---------------------------------------------------------------- real values never reach the AI
def test_question_with_a_name_never_reaches_prompt_log_or_cache_key():
    question = "why is Rahul Sharma's page slow"
    with fresh_audit_log() as path:
        r = pipeline.analyze(question)
        prompt = llm.get_audit_log()[-1]["prompt"]
        key = pipeline.cache_key(r["match"], r["masked_query"])
        print(f"      question : {question!r} -> matched {r['match']['query_id']} ({r['match']['confidence']:.0%})")
        print(f"      prompt   : {len(prompt)} chars, first line of query part: "
              f"{prompt.split('Slow query (masked):')[1].strip().splitlines()[0]!r}")
        print(f"      cache key: {key}")
        for name, text in {"prompt": prompt, "audit log": everything_logged(path),
                           "cache key": key, "match": json.dumps(r["match"]),
                           "masked_query": r["masked_query"], "explanation": r["explanation"]}.items():
            assert "rahul" not in text.lower() and "sharma" not in text.lower(), f"name found in {name}"


def test_cache_key_ignores_wording_of_the_question():
    a = pipeline.match_question("Why is the weekly sales report slow?")[1]
    b = pipeline.match_question("weekly sales dashboard timing out")[1]
    assert pipeline.cache_key(a, "x") == pipeline.cache_key(b, "y") == "known:q11"
    k1 = pipeline.cache_key({"query_id": "adhoc"}, "SELECT ? FROM t_a")
    assert k1 == pipeline.cache_key({"query_id": "adhoc"}, "SELECT ? FROM t_a") and k1.startswith("adhoc:")
    assert k1 != pipeline.cache_key({"query_id": "adhoc"}, "SELECT ? FROM t_b")


PASTED = "SELECT * FROM customers WHERE name = 'Rahul Sharma' AND email = 'rahul@example.com'"


def test_broken_masker_is_stopped_before_the_ai():
    """If the masker leaves real values in, stop BEFORE ai/ or the LLM see them."""
    with fresh_audit_log() as path, patched(mask, "mask_query", lambda sql, m: sql):
        try:
            pipeline.analyze(PASTED)
        except pipeline.PrivacyError as e:
            print(f"      stopped: {e}")
            assert str(e) == pipeline.BLOCKED_MESSAGE and "may not be masked" in str(e)
        else:
            raise AssertionError("leaky masked SQL was not stopped")
        log = llm.get_audit_log()
        assert [e["event"] for e in log] == ["analysis_blocked"], "LLM was reached or event missing"
        print(f"      audit event: {log[0]}")
        assert log[0]["strict"] is True and log[0]["query_id"] == "adhoc"
        assert "rahul" not in everything_logged(path), "leaked text was stored"


def test_non_strict_mode_warns_but_the_llm_prompt_is_still_blocked():
    """PRIVACY_STRICT=0: analysis continues, event is logged, but the prompt check still holds."""
    with fresh_audit_log() as path, patched(mask, "mask_query", lambda sql, m: sql), \
            patched(pipeline, "PRIVACY_STRICT", False):
        r = pipeline.analyze(PASTED)
        events = [e["event"] for e in llm.get_audit_log()]
        assert events == ["analysis_warned", "llm_call"], events
        assert r["llm"]["prompt_leak_ok"] is False and r["leak_check"] is False
        # The leak is only in the masked query, which the template text never contains,
        # so the clean template is shown (not the generic "withheld" sentence).
        assert r["llm"]["source"] == "template" and "rahul" not in r["explanation"].lower()
        assert "rahul" not in everything_logged(path)


def test_leak_added_by_the_ai_side_is_blocked_at_the_llm():
    """The AI step itself puts a real value into its reason: prompt must be blocked."""
    def leaky_rl(masked_sql, masked_plan, cost_fn):
        return [{"kind": "index", "action": "CREATE INDEX ON t_a3f (c_91b)", "cost_before": 100.0,
                 "cost_after": 10.0, "reason": "Rows for Rahul Sharma are scanned."}]
    with fresh_audit_log() as path, patched(rl, "recommend", leaky_rl):
        r = pipeline.analyze(PASTED)
        entry = llm.get_audit_log()[-1]
        print(f"      prompt_leak_ok={r['llm']['prompt_leak_ok']} source={r['llm']['source']} "
              f"pipeline leak_check={r['leak_check']}")
        print(f"      audit prompt  : {entry['prompt']}")
        assert r["llm"]["prompt_leak_ok"] is False and entry["sent"] is False
        assert r["explanation"] == llm.SAFE_TEXT
        assert "rahul" not in everything_logged(path)


# ---------------------------------------------------------------- llm.py itself
def checker(text):
    return not any(w in text.lower() for w in ("orders", "rahul"))


def sample(reason="Filter columns have no index."):
    return {"masked_query": "SELECT ... FROM t_a3f WHERE c_91b = ?",
            "masked_plan": {"Plan": {"Node Type": "Seq Scan", "Relation Name": "t_a3f",
                                     "Total Cost": 19437.0, "Plan Rows": 1200, "Plans": []}},
            "bottleneck": {"op": "Seq Scan", "reason": "Full scan.", "evidence": {"table": "t_a3f"}},
            "recommendations": [{"kind": "index", "action": "CREATE INDEX ON t_a3f (c_91b)",
                                 "cost_before": 19437.0, "cost_after": 812.0, "reason": reason}]}


def test_prompt_uses_only_the_four_allowed_fields():
    r = sample()
    r["bitmask"] = "SENTINEL_BITMASK"
    r["something_else"] = "SENTINEL_EXTRA"
    r["masked_plan"]["Plan"]["Filter"] = "SENTINEL_FILTER"          # plan filters can hold literals
    r["masked_plan"]["Plan"]["Index Cond"] = "SENTINEL_INDEXCOND"
    prompt = llm.build_prompt(r)
    assert "SENTINEL" not in prompt
    for must in ("SELECT ... FROM t_a3f", "Seq Scan on t_a3f", "Full scan.", "CREATE INDEX ON t_a3f (c_91b)"):
        assert must in prompt, must


def test_fake_llm_clean_reply_is_used():
    with fresh_audit_log(), patched(llm, "_call_ollama", lambda p: "  The table is scanned fully; add the index.  "):
        out = llm.explain(sample(), checker)
        assert (out["source"], out["reply_leak_ok"], out["prompt_leak_ok"]) == ("ollama", True, True)
        assert out["text"] == "The table is scanned fully; add the index."
        assert llm.get_audit_log()[-1]["sent"] is True


def test_fake_llm_leaky_reply_is_thrown_away_and_not_stored():
    with fresh_audit_log() as path, patched(llm, "_call_ollama", lambda p: "Look at the orders table for Rahul."):
        out = llm.explain(sample(), checker)
        print(f"      source={out['source']} reply_leak_ok={out['reply_leak_ok']} reason={out['fallback_reason']}")
        assert (out["source"], out["reply_leak_ok"]) == ("template", False)
        assert checker(out["text"])
        assert "rahul" not in everything_logged(path)
        assert llm.get_audit_log()[-1]["reply"].startswith("[BLOCKED")


def test_crashing_checker_fails_closed():
    def boom(text):
        raise RuntimeError("checker down")
    with fresh_audit_log():
        out = llm.explain(sample(), boom)
        assert out["prompt_leak_ok"] is False and out["text"] == llm.SAFE_TEXT


def test_audit_log_file_matches_memory():
    with fresh_audit_log() as path:
        llm.explain(sample(), checker)
        llm.explain(sample("Another reason."), checker)
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        assert lines == llm.get_audit_log() and len(lines) == 2
        assert {"ts", "source", "sent", "prompt", "prompt_leak_ok", "reply", "reply_leak_ok",
                "latency_ms", "fallback_reason"} <= set(lines[0])


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
