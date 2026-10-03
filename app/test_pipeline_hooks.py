"""Tests for the hooks that connect ai/ to the database (Step 3.5): cost_fn, time_fn,
stats, column_types.  All use FAKE numbers and fake functions; names come from the REAL masker's mapping, and the
slow real measurements are replaced by fast fakes.

Run:  .venv/Scripts/python -m app.test_pipeline_hooks      (from the project folder)
"""
import json
from contextlib import ExitStack, contextmanager

import pipeline
from ai import rl
from app import llm
from app.test_privacy import fresh_audit_log
from db import sandbox
from privacy import mask

_MISSING = object()


@contextmanager
def patched(obj, name, value):
    """Like a normal monkeypatch, but also works when the attribute does not exist yet
    (e.g. sandbox.checked_run before Person 1 delivers it). value=_MISSING removes it."""
    old = getattr(obj, name, _MISSING)
    if value is _MISSING:
        if old is not _MISSING:
            delattr(obj, name)
    else:
        setattr(obj, name, value)
    try:
        yield
    finally:
        if old is _MISSING:
            if hasattr(obj, name):
                delattr(obj, name)
        else:
            setattr(obj, name, old)


# ---------------------------------------------------------------- 1) cost_fn
ORIGINAL = "SELECT * FROM orders WHERE region_id = 3"        # the admin's real SQL
MAPPING = mask.build_mapping()
# The REAL masked names (they come from the secret key, so we look them up, never hard-code them).
T = MAPPING.tables["orders"]
C_REGION, C_DATE, C_ORDER = (MAPPING.columns[c] for c in ("region_id", "order_date", "order_id"))
MASKED = f"SELECT * FROM {T} WHERE {C_REGION} = $1"


def test_cost_fn_costs_index_actions_and_unmasks_them():
    calls = []
    with patched(sandbox, "cost", lambda sql, idx=[]: calls.append((sql, list(idx))) or 123.0):
        cost_fn = pipeline.make_cost_fn(ORIGINAL, MASKED, MAPPING)
        assert cost_fn(MASKED, [f"CREATE INDEX ON {T} ({C_REGION}, {C_DATE})"]) == 123.0
        assert cost_fn(MASKED, [f"CREATE UNIQUE INDEX ON {T} ({C_REGION});"]) == 123.0
        assert cost_fn(MASKED, []) == 123.0
    print(f"      sandbox.cost got: {calls[0]}")
    # the original query goes in exactly as written; the index names are un-masked privately
    assert calls[0] == (ORIGINAL, ["CREATE INDEX ON orders (region_id, order_date)"])
    assert calls[2] == (ORIGINAL, [])


def test_cost_fn_refuses_anything_but_create_index():
    calls = []
    with patched(sandbox, "cost", lambda sql, idx=[]: calls.append(1) or 1.0):
        cost_fn = pipeline.make_cost_fn(ORIGINAL, MASKED, MAPPING)
        for bad in ["PARTITION t_a3f BY RANGE (c_d42) YEARLY",          # only MONTHLY is simulated
                    "PARTITION t_a3f BY RANGE (c_d42) MONTHLY; DROP TABLE t_a3f",
                    "DROP TABLE t_a3f",
                    "CREATE INDEX ON t_a3f (c_91b); DROP TABLE t_a3f",
                    "CREATE TABLE t_x AS SELECT 1",
                    ""]:
            try:
                cost_fn(MASKED, [bad])
            except ValueError as e:
                print(f"      refused {bad[:44]!r} -> ValueError")
                continue
            raise AssertionError(f"accepted: {bad!r}")
        try:                                   # one bad action poisons the whole call
            cost_fn(MASKED, ["CREATE INDEX ON t_a3f (c_91b)", "PARTITION t_a3f BY RANGE (c_d42) YEARLY"])
        except ValueError:
            pass
        else:
            raise AssertionError("mixed list accepted")
    assert calls == [], "sandbox.cost was reached for a refused action"


def test_cost_fn_costs_a_monthly_partition_action():
    calls = []
    with patched(sandbox, "cost", lambda sql, idx=[]: calls.append((sql, list(idx))) or 7.0):
        cost_fn = pipeline.make_cost_fn(ORIGINAL, MASKED, MAPPING)
        assert cost_fn(MASKED, [f"PARTITION {T} BY RANGE ({C_DATE}) MONTHLY"]) == 7.0
    assert calls == [(ORIGINAL, ["PARTITION orders BY RANGE (order_date) MONTHLY"])], calls


def test_cost_fn_unmasks_a_rewritten_query():
    calls = []
    with patched(sandbox, "cost", lambda sql, idx=[]: calls.append(sql) or 1.0):
        cost_fn = pipeline.make_cost_fn(ORIGINAL, MASKED, MAPPING)
        cost_fn(f"SELECT {C_REGION} FROM {T}", [])
    assert calls == ["SELECT region_id FROM orders"], calls


# ================================================================
# Whole-pipeline tests with a FAKE ai/rl.recommend and fake db/privacy functions
# ================================================================
class FakeRL:
    """Stands in for P3's recommend(): records what it receives, and (like P3's
    verify_rewrite) calls time_fn for the original and for each rewrite."""

    def __init__(self, rewrites=()):
        self.rewrites, self.calls, self.time_answers = list(rewrites), [], []

    def __call__(self, masked_sql, masked_plan, cost_fn, stats=None, time_fn=None, agent=None,
                 column_types=None):
        self.calls.append({"stats": stats, "time_fn": time_fn, "column_types": column_types})
        recs = [{"kind": "index", "action": f"CREATE INDEX ON {T} ({C_REGION})",
                 "cost_before": 100.0, "cost_after": 10.0, "reason": "no index"}]
        if time_fn:
            self.time_answers.append(time_fn(masked_sql))
            for sql in self.rewrites:
                answer = time_fn(sql)
                self.time_answers.append(answer)
                recs.insert(0, {"kind": "rewrite", "action": sql, "cost_before": 100.0,
                                "cost_after": 5.0, "reason": "rewrite"})
        return recs


def fake_checked_run(sql):
    """Fake Person-1 function. The words in the SQL decide the answer."""
    if "DIFFROWS" in sql:
        return {"ms": 5.0, "rows": 9, "checksum": "AAA"}      # fewer rows, same hash
    if "DIFF" in sql:
        return {"ms": 5.0, "rows": 10, "checksum": "BBB"}     # same count, other values
    if "SAME" in sql:
        return {"ms": 20.0, "rows": 10, "checksum": "AAA"}
    return {"ms": 100.0, "rows": 10, "checksum": "AAA"}       # the original query


def _no_ollama(prompt):
    raise llm.LLMUnavailable("Ollama is off in this test")


FAST_MEASUREMENTS = [(sandbox, "timed_run", lambda sql, idx=[]: 10.0),
                     (sandbox, "measure_write_latency", lambda index_sql: {"delta_ms": 0.001}),
                     (sandbox, "index_size_bytes", lambda index_sql: 1_000_000)]


def analyze_with(fake_rl, *patches):
    """Run pipeline.analyze with a fake recommend() plus extra (obj, name, value) patches.
    Returns (result, audit log)."""
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (rl, "recommend", fake_rl), *FAST_MEASUREMENTS, *patches]:
            stack.enter_context(patched(obj, name, value))
        result = pipeline.analyze("Why is the weekly sales report slow?")
        return result, llm.get_audit_log()


NO_HOOKS = [(sandbox, "checked_run", _MISSING), (sandbox, "table_stats", _MISSING),
            (mask, "mask_stats", _MISSING), (mask, "column_types", _MISSING)]


def events(log):
    return [e["event"] for e in log if e["event"] != "llm_call"]


# ---------------------------------------------------------------- nothing available yet
def test_hooks_off_until_the_other_modules_exist():
    fake = FakeRL()
    r, log = analyze_with(fake, *NO_HOOKS)
    print(f"      ai_inputs: {r['ai_inputs']}")
    assert fake.calls == [{"stats": None, "time_fn": None, "column_types": None}]
    assert r["ai_inputs"] == {"time_fn": "not available yet", "stats": "not available yet",
                              "column_types": "not available yet"}
    assert r["unsafe_rewrite"] is False and events(log) == []


def test_old_style_recommend_without_extra_arguments_still_works():
    """The stub ai/rl.recommend takes only 3 arguments. Nothing may be built or passed to it."""
    built = []
    patches = [(sandbox, "checked_run", lambda sql: built.append("run") or fake_checked_run(sql)),
               (sandbox, "table_stats", lambda sql=None: built.append("stats") or {}),
               (mask, "mask_stats", lambda s, m: s), (mask, "column_types", lambda m: {})]
    def three_argument_recommend(masked_sql, masked_plan, cost_fn):          # the old signature
        return [{"kind": "index", "action": f"CREATE INDEX ON {T} ({C_REGION})", "cost_before": 100.0,
                 "cost_after": 10.0, "reason": "no index"}]
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (rl, "recommend", three_argument_recommend), *FAST_MEASUREMENTS, *patches]:
            stack.enter_context(patched(obj, name, value))
        r = pipeline.analyze("Why is the weekly sales report slow?")
    assert built == [], f"built inputs the AI cannot take: {built}"
    assert set(r["ai_inputs"].values()) == {"not supported by ai/ yet"}


# ---------------------------------------------------------------- 2) time_fn
def test_time_fn_gives_ai_only_ms_and_results_match():
    fake = FakeRL(rewrites=[f"SELECT SAME FROM {T}"])
    r, log = analyze_with(fake, (sandbox, "checked_run", fake_checked_run))
    print(f"      P3 received: {fake.time_answers}")
    assert fake.time_answers == [{"ms": 100.0, "results_match": True}, {"ms": 20.0, "results_match": True}]
    assert all(set(a) == {"ms", "results_match"} for a in fake.time_answers)       # no rows, no checksum
    assert r["ai_inputs"]["time_fn"] == "used" and r["unsafe_rewrite"] is False
    assert not any(x.get("unsafe_rewrite") for x in r["recommendations"])


def test_different_checksums_flag_an_unsafe_rewrite_and_demote_it():
    for bad in (f"SELECT DIFF FROM {T}", f"SELECT DIFFROWS FROM {T}"):          # other values / other count
        fake = FakeRL(rewrites=[f"SELECT SAME FROM {T}", bad])   # the bad one ends up FIRST in P3's list
        r, log = analyze_with(fake, (sandbox, "checked_run", fake_checked_run))
        recs = r["recommendations"]
        print(f"      {bad[:22]!r}: P3 got {fake.time_answers[-1]}; order now "
              f"{[x['action'][:22] for x in recs]}; flag={r['unsafe_rewrite']}")
        assert fake.time_answers[-1] == {"ms": 5.0, "results_match": False}
        assert r["unsafe_rewrite"] is True
        assert recs[-1]["action"] == bad and recs[-1]["unsafe_rewrite"] is True
        assert "UNSAFE" in recs[-1]["warning"]
        assert recs[0]["action"] != bad and not recs[0].get("unsafe_rewrite")      # best is a safe fix
        # checksums and row counts never leave pipeline.py
        everything = json.dumps(r, default=str) + json.dumps(log) + json.dumps(fake.time_answers)
        assert "AAA" not in everything and "BBB" not in everything and "checksum" not in everything


def test_failed_timing_run_retries_without_time_fn_and_logs_only_the_error_type():
    def broken(sql):
        raise RuntimeError("could not run: orders Rahul")           # message holds real names
    fake = FakeRL(rewrites=[f"SELECT SAME FROM {T}"])
    r, log = analyze_with(fake, (sandbox, "checked_run", broken))
    ev = [e for e in log if e["event"] == "time_fn_failed"]
    print(f"      audit: {ev[0]}")
    assert len(fake.calls) == 2 and fake.calls[0]["time_fn"] is not None and fake.calls[1]["time_fn"] is None
    assert r["ai_inputs"]["time_fn"] == "failed: rewrites are unverified"
    assert ev[0]["error"] == "RuntimeError" and "rahul" not in json.dumps(log).lower()


# ---------------------------------------------------------------- 3) stats
RAW_STATS = {"rows": {"orders": 1_000_000.0},
             "n_distinct": {"orders.region_id": 10, "orders.order_date": -0.001},
             "existing_indexes": [("orders", ("order_id",)), ("orders", ("region_id",))]}


def fake_mask_stats(stats, mapping):
    """Fake Person-2 function: swaps real names for masked ones using the private mapping."""
    m = mapping.real_to_masked

    def name(key):
        return ".".join(m.get(part, part) for part in key.split("."))
    return {"rows": {name(t): n for t, n in stats["rows"].items()},
            "n_distinct": {name(k): n for k, n in stats["n_distinct"].items()},
            "existing_indexes": [(name(t), tuple(m.get(c, c) for c in cols))
                                 for t, cols in stats["existing_indexes"]]}


def test_stats_are_masked_checked_completed_and_passed():
    fake = FakeRL()
    r, log = analyze_with(fake, (sandbox, "table_stats", lambda sql=None: RAW_STATS), (mask, "mask_stats", fake_mask_stats))
    got = fake.calls[0]["stats"]
    print(f"      P3 received stats: {got}")
    assert r["ai_inputs"]["stats"] == "used"
    assert got["rows"] == {T: 1_000_000.0}
    assert got["existing_indexes"] == [(T, (C_ORDER,)), (T, (C_REGION,))]
    assert got["eq_sel"] == {f"{T}.{C_REGION}": 0.1, f"{T}.{C_DATE}": 0.001}      # 1/10 and 1/(0.001*1e6)
    assert got["range_sel"] == {} and "orders" not in json.dumps(got)
    assert events(log) == []


def test_stats_with_a_real_name_or_value_are_dropped_and_logged_not_fatal():
    leaks = {
        "unmasked table name": lambda s, m: {**fake_mask_stats(s, m), "rows": {"orders": 1.0}},
        "real value from the query": lambda s, m: {**fake_mask_stats(s, m),
                                                   "existing_indexes": [(T, ("small_business",))]},
    }
    for why, bad_masker in leaks.items():
        fake = FakeRL()
        r, log = analyze_with(fake, (sandbox, "table_stats", lambda sql=None: RAW_STATS), (mask, "mask_stats", bad_masker))
        ev = [e for e in log if e["event"] == "stats_dropped"]
        print(f"      {why}: ai_inputs={r['ai_inputs']['stats']!r}; audit={ev[0]}")
        assert fake.calls[0]["stats"] is None                              # P3 never saw them
        assert r["ai_inputs"]["stats"] == "dropped: failed the leak check" and len(ev) == 1
        assert r["recommendations"] and r["leak_check"] is True            # analysis carried on, clean
        text = json.dumps(log).lower()
        assert "small_business" not in text and '"orders"' not in text      # logged: sizes and hash only


def test_stats_that_crash_are_skipped_and_logged_by_type_only():
    def boom(sql=None):
        raise ConnectionError("db down at orders.customer_email Rahul")
    fake = FakeRL()
    r, log = analyze_with(fake, (sandbox, "table_stats", boom), (mask, "mask_stats", fake_mask_stats))
    ev = [e for e in log if e["event"] == "stats_unavailable"]
    assert fake.calls[0]["stats"] is None and r["ai_inputs"]["stats"] == "unavailable"
    assert ev[0]["error"] == "ConnectionError" and "rahul" not in json.dumps(log).lower()


def test_complete_stats_fills_what_p3_indexes_directly():
    done = pipeline._complete_stats({"rows": {"t_a": 1000.0}, "n_distinct": {"t_a.c_x": -1, "t_a.c_y": 4, "t_a.c_z": 0}})
    assert done["eq_sel"] == {"t_a.c_x": 0.001, "t_a.c_y": 0.25}     # -1 means "all rows are different"
    assert done["range_sel"] == {} and done["existing_indexes"] == []
    keep = pipeline._complete_stats({"rows": {}, "eq_sel": {"t_a.c_x": 0.5}, "n_distinct": {"t_a.c_x": 2}})
    assert keep["eq_sel"] == {"t_a.c_x": 0.5}                         # never overwrite what P2/P1 gave


# ---------------------------------------------------------------- 4) column_types
def test_column_types_pass_through_when_the_masker_has_them_else_skip():
    fake = FakeRL()
    r, _ = analyze_with(fake, *NO_HOOKS)
    assert fake.calls[0]["column_types"] is None and r["ai_inputs"]["column_types"] == "not available yet"
    types = {f"{T}.{C_REGION}": "number", f"{T}.{C_DATE}": "date"}
    fake = FakeRL()
    r, _ = analyze_with(fake, (mask, "column_types", lambda m: types))
    assert fake.calls[0]["column_types"] == types and r["ai_inputs"]["column_types"] == "used"
    fake = FakeRL()
    r, log = analyze_with(fake, (mask, "column_types", lambda m: {"orders.region_id": "number"}))   # a real name as a KEY
    assert fake.calls[0]["column_types"] is None and "column_types_dropped" in events(log)


# ---------------------------------------------------------------- 5) prompt wording
def test_prompt_mentions_both_placeholder_styles():
    assert '"?"' not in llm.PROMPT_TEMPLATE
    prompt = llm.build_prompt({"masked_query": "SELECT c_1 FROM t_1 WHERE c_2 = $1", "masked_plan": {},
                               "bottleneck": {}, "recommendations": []})
    assert "? or $1, $2" in prompt and "SELECT c_1 FROM t_1 WHERE c_2 = $1" in prompt


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
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
