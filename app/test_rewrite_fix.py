"""Tests for Step 2 of the ai/ update: a fix timed together with a query rewrite (rewrite_sql), and ai/'s
measured write latency.

Run:  .venv/Scripts/python -m app.test_rewrite_fix      (from the project folder)

The rewrite is a SELECT: it is shown as "replace the query with this one", as COMMENTS in the Approve script,
never as something to run. Real values appear only inside the script, only after the click.
"""
from app import approve, tradeoff
from app.test_approve import approve_buttons, approve_result, page_with, script_on
from app.test_before_after import index_result
from app.test_recommendations import page_text, run_page

REAL_REWRITE = "SELECT count(*) FROM customers c WHERE c.name = 'Rahul Sharma' AND c.customer_id = 90210"
MASKED_REWRITE = "SELECT count(*) FROM t_491 AS a1 WHERE a1.c_2e4 = $1 AND a1.c_720 = $2"
INDEX = "CREATE INDEX ON orders (region_id)"
RULE = "filter join to exists"


def result_with_rewrite():
    r = approve_result()
    top = r["recommendations"][0]                        # the measured fix (the top pick)
    top["rewrite_sql"], top["rewrite_rule"] = MASKED_REWRITE, RULE
    r["deploy_rewrites"] = [REAL_REWRITE, "", ""]
    return r


# ---------------------------------------------------------------- the script text
def test_the_rewrite_is_written_as_comments_and_never_as_a_statement():
    script = approve.build_script("index", INDEX, "measured", REAL_REWRITE + ";", RULE)
    lines = script.splitlines()
    body = lines[lines.index("BEGIN;") + 1:-1]
    statements = [x for x in body if not x.startswith("--")]
    assert statements == [INDEX + ";"], statements                                  # only the DDL is executable
    assert any(RULE in x and x.startswith("-- ALSO: replace the query") for x in body)
    assert f"--   {REAL_REWRITE}" in body and lines[-1].startswith("ROLLBACK;")


def test_a_hostile_rewrite_stays_inside_comments():
    nasty = "SELECT 1;\nDROP TABLE orders;\n-- and more"
    body = approve.build_script("index", INDEX, "", nasty, "x\nDROP TABLE y").splitlines()
    assert not [x for x in body if not x.startswith("--") and "DROP" in x]
    assert all(x.startswith("--") or x.strip() in ("BEGIN;", INDEX + ";") or x.startswith("ROLLBACK;") for x in body)


def test_no_rewrite_means_the_script_is_exactly_what_it_was_before():
    assert approve.build_script("index", INDEX, "m") == approve.build_script("index", INDEX, "m", "", "")
    assert "ALSO" not in approve.build_script("index", INDEX, "m", None, None)


def test_nothing_after_a_newline_can_escape_a_comment_or_become_a_statement():
    def executable(script):
        lines = script.splitlines()
        return [x for x in lines[lines.index("BEGIN;") + 1:-1] if not x.startswith("--")]
    # the kind, the label, a partition's text, an index with a hidden second line
    s = approve.build_script("index", INDEX, "m\nDROP TABLE b")              # a hostile label
    assert executable(s) == [INDEX + ";"] and "DROP" not in "\n".join(x for x in s.splitlines() if not x.startswith("--"))
    s = approve.build_script("index\nDROP TABLE a", INDEX)                   # a hostile kind is not "index": nothing runs
    assert executable(s) == [] and "DROP" not in "\n".join(x for x in s.splitlines() if not x.startswith("--"))
    s = approve.build_script("partition", "PARTITION orders BY RANGE (order_date) MONTHLY\nDROP TABLE c")
    assert executable(s) == []
    s = approve.build_script("partition", "PARTITION orders BY RANGE (d) MONTHLY; CREATE INDEX ON orders (a)\nDROP TABLE e")
    assert executable(s) == [] and "DROP TABLE e" not in "\n".join(x for x in s.splitlines() if not x.startswith("--"))
    s = approve.build_script("index", "CREATE INDEX ON orders (a)\nDROP TABLE f")
    assert executable(s) == []
    # a "rewrite" that is not exactly one SELECT is shown as comments only; a real SELECT is still a statement
    for bad in ("SELECT 1; DROP TABLE g", "DROP TABLE h", "SELECT 1 INTO x", "SELECT * FROM t FOR UPDATE"):
        assert executable(approve.build_script("rewrite", bad)) == [], bad
    assert executable(approve.build_script("rewrite", "SELECT count(*) FROM orders")) == ["SELECT count(*) FROM orders;"]


def test_rewrite_for_follows_the_alignment_and_ignores_junk():
    r = result_with_rewrite()
    recs = r["recommendations"]
    assert approve.rewrite_for(r, recs[0]) == REAL_REWRITE and approve.rewrite_for(r, recs[1]) == ""
    assert approve.rewrite_for(r, {"kind": "stranger"}) == "" and approve.rewrite_for({}, recs[0]) == ""
    assert approve.rewrite_for({**r, "deploy_rewrites": "x"}, recs[0]) == ""
    assert approve.rewrite_for({**r, "deploy_rewrites": [5]}, recs[0]) == ""


# ---------------------------------------------------------------- the page
def test_the_card_shows_the_masked_rewrite_and_the_real_one_only_inside_the_script_after_the_click():
    with page_with(result_with_rewrite()) as at:
        before = page_text(at)
        assert MASKED_REWRITE in [c.value for c in at.code] and "Also rewrite the query (" + RULE + ")" in before
        assert "Rahul" not in before and "90210" not in before and "rewrite rule" in before
        approve_buttons(at)[0].click().run()
        assert not at.exception, [e.value for e in at.exception]
        script = script_on(at)
        assert f"--   {REAL_REWRITE}" in script and "(rewrite rule: " + RULE + ")" in script
        rest = page_text(at).replace(script, "")
        assert "Rahul" not in rest and "90210" not in rest, "a real value leaked outside the script"


# ---------------------------------------------------------------- ai/'s measured write latency
def test_the_write_card_uses_ais_measured_write_latency_and_ignores_an_estimate():
    measured = {"source": "measured", "before_ms_per_row": 0.01, "delta_ms_per_row": 0.004, "pct": 40.0, "indexes": 1}
    n = tradeoff.resolve(index_result(write=0.5, write_latency=measured))
    assert n["write"] == 0.004                                                       # the rec's own number wins
    estimate = {"source": "estimated", "delta_ms_per_row": 0.9, "note": "width-based, not measured"}
    assert tradeoff.resolve(index_result(write=0.5, write_latency=estimate))["write"] == 0.5
    assert tradeoff.resolve(index_result(write=None, write_latency=estimate))["write"] is None
    assert tradeoff.resolve(index_result(write=0.5, write_latency="junk"))["write"] == 0.5
    text = page_text(run_page(index_result(write=0.5, write_latency=measured)))
    assert "+0.004 ms per insert" in text and "0.5 ms per insert" not in text


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
