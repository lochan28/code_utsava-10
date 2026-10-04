"""Tests for Step 1 of the ai/ update: time_fn(masked_sql, actions=()).

Run:  .venv/Scripts/python -m app.test_time_actions      (from the project folder; needs the Docker database)

time_fn runs a query on the sandbox copy with a fix (CREATE INDEX / one MONTHLY partition) applied,
and returns ONLY {"ms", "results_match"} to ai/. Real names and values go back in only for the sandbox run.
"""
import inspect

import pipeline
from db import sandbox
from privacy import mask
from app.test_privacy import patched

ORIG = "SELECT count(*) FROM orders WHERE customer_id = 34950 AND status = 'delivered'"
DIFFERENT = "SELECT count(*) FROM orders WHERE customer_id = 34950 OR status = 'delivered'"
M = mask.build_mapping()
T, C_CUST, C_DATE = M.tables["orders"], M.columns["customer_id"], M.columns["order_date"]
INDEX = f"CREATE INDEX ON {T} ({C_CUST})"
PARTITION = f"PARTITION {T} BY RANGE ({C_DATE}) MONTHLY"


def make(spy=None):
    masked = mask.mask_query(ORIG, M)
    checks = []
    tf = pipeline.make_time_fn(ORIG, masked, M, checks, pipeline._restorer(ORIG, M))
    return tf, masked, checks


def test_time_fn_has_an_actions_parameter_with_an_empty_default():
    tf, _, _ = make()
    p = inspect.signature(tf).parameters
    assert "actions" in p and p["actions"].default == ()


def test_a_fix_is_timed_and_only_ms_and_results_match_go_to_ai():
    tf, masked, checks = make()
    base = tf(masked, actions=[])
    fixed = tf(masked, actions=[INDEX])
    print(f"      original {base['ms']} ms -> with the index {fixed['ms']} ms")
    assert set(base) == set(fixed) == {"ms", "results_match"}
    assert base["results_match"] is True and fixed["results_match"] is True
    assert fixed["ms"] < base["ms"]
    assert checks[-1]["actions"] == [INDEX]                       # the log keeps the MASKED action


def test_a_monthly_partition_can_be_timed_too():
    tf, masked, _ = make()
    assert tf(masked, actions=[PARTITION])["results_match"] is True


def test_a_rewrite_that_returns_different_rows_is_flagged_even_with_a_fix():
    tf, masked, _ = make()
    other = mask.mask_query(DIFFERENT, M)
    assert tf(other)["results_match"] is False
    assert tf(other, actions=[INDEX])["results_match"] is False


def test_actions_that_are_not_one_index_or_one_monthly_partition_are_refused_before_the_sandbox():
    calls = []
    with patched(sandbox, "checked_run", lambda *a, **k: calls.append(a)):
        tf, masked, _ = make()                     # built INSIDE the patch: it reads sandbox.checked_run once
        for bad in ("DROP TABLE orders", f"CREATE INDEX ON {T} ({C_CUST}); DROP TABLE orders",
                    f"PARTITION {T} BY RANGE ({C_DATE}) YEARLY", "SELECT 1"):
            try:
                tf(masked, actions=[bad])
                raise AssertionError(f"accepted: {bad}")
            except ValueError:
                pass
    assert calls == [], "the sandbox was called for a refused action"


def test_a_fix_the_sandbox_cannot_build_is_a_valueerror_not_a_dead_time_fn():
    tf, masked, _ = make()
    for bad in (f"CREATE INDEX ON {T} (no_such_column)", f"CREATE INDEX ON no_such_table ({C_CUST})"):
        try:
            tf(masked, actions=[bad])
            raise AssertionError("no error")
        except ValueError as e:
            assert "no_such" not in str(e)                         # the message carries nothing from the sql
    assert tf(masked, actions=[INDEX])["results_match"] is True    # and time_fn still works afterwards


def test_the_same_question_is_run_once_and_other_failures_stay_timechecks():
    real, calls = sandbox.checked_run, []

    def spy(sql, index_sqls=[]):
        calls.append((sql, tuple(index_sqls)))
        return real(sql, index_sqls)
    with patched(sandbox, "checked_run", spy):
        tf, masked, _ = make()                     # built INSIDE the patch: it reads sandbox.checked_run once
        for _ in range(5):
            tf(masked, actions=[INDEX])
    assert len(calls) == 2, calls                                  # the baseline + the fix, once each

    def boom(sql, index_sqls=[]):
        raise RuntimeError("timeout with a Rahul Sharma in it")
    with patched(sandbox, "checked_run", boom):
        tf2, masked2, _ = make()
        try:
            tf2(masked2, actions=[INDEX])
            raise AssertionError("no error")
        except pipeline.TimeCheckError as e:
            assert str(e) == "RuntimeError" and "Rahul" not in str(e)


def test_the_sandbox_gets_real_names_in_actions_and_ai_never_does():
    real, seen = sandbox.checked_run, []

    def spy(sql, index_sqls=[]):
        seen.append((sql, list(index_sqls)))
        return real(sql, index_sqls)
    with patched(sandbox, "checked_run", spy):
        tf, masked, _ = make()
        answer = tf(masked, actions=[INDEX])
    sql, acts = seen[-1]
    assert acts == ["CREATE INDEX ON orders (customer_id)"] and T not in acts[0]   # the sandbox: real names
    assert sql == ORIG and "$" not in sql
    assert T not in str(answer) and "orders" not in str(answer)                    # ai/: numbers only


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
