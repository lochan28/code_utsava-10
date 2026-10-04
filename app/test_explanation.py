"""Tests: the explanation (LLM prompt and template fallback) describes the page's BEST FIX, with its
MEASURED numbers first and the planner estimate second. An unsafe fix is never a top pick.

Run:  .venv/Scripts/python -m app.test_explanation      (from the project folder)
"""
from contextlib import ExitStack

import pipeline
from ai import gnn, rl
from app import llm
from app.test_privacy import fresh_audit_log, patched
from app.test_support import stand_in_bottleneck
from app.test_tail import INDEX_M, PART_M, rec

PARTITION = {"kind": "partition", "action": "PARTITION t_1 BY RANGE (c_2) MONTHLY", "cost_before": 1000.0,
             "cost_after": 60.0, "reason": "reads only the months it needs", "verified_by": "timed_run",
             "measured_gain_pct": 62.3, "measured_ms_before": 110.4, "measured_ms_after": 41.6}
INDEX = {"kind": "index", "action": "CREATE INDEX ON t_1 (c_3)", "cost_before": 1000.0, "cost_after": 820.0,
         "reason": "a small index", "verified_by": "hypopg_cost"}
UNSAFE = {"kind": "rewrite", "action": "SELECT 1", "cost_before": 1000.0, "cost_after": 10.0, "reason": "faster",
          "verified_by": "timed_run", "unsafe_rewrite": True}


def result(*recs):
    return {"masked_query": "SELECT 1", "masked_plan": {}, "bottleneck": {"op": "Seq Scan", "reason": "r"},
            "recommendations": list(recs)}


def test_only_the_first_fix_is_the_top_pick_and_the_others_are_alternatives():
    lines = llm._rec_lines([PARTITION, INDEX]).splitlines()
    assert lines[0].startswith("1. BEST FIX: [partition]") and lines[1].startswith("2. alternative: [index]")
    assert sum("BEST FIX" in x for x in lines) == 1
    prompt = llm.build_prompt(result(PARTITION, INDEX))
    assert "fix 1 is the BEST FIX" in prompt and "Never call another fix the best one" in prompt
    assert "Do not mention fix numbers" in prompt


def test_measured_numbers_come_first_and_the_planner_estimate_second():
    top = llm._rec_lines([PARTITION, INDEX]).splitlines()[0]
    assert "measured 62% faster (110 ms -> 42 ms)" in top and "planner estimate: cost 1,000 -> 60 (94% lower)" in top
    assert top.index("measured 62%") < top.index("planner estimate")
    second = llm._rec_lines([PARTITION, INDEX]).splitlines()[1]
    assert "measured" not in second and "planner estimate: cost 1,000 -> 820 (18% lower)" in second   # no measured gain: as before
    assert "measured" not in llm._rec_lines([{**PARTITION, "measured_gain_pct": "62"}])               # junk is not a measurement
    assert "62% faster" in llm._rec_lines([{**PARTITION, "measured_ms_after": None}])                  # a gain without times is fine


def test_an_unsafe_fix_is_never_called_the_top_pick():
    lines = llm._rec_lines([UNSAFE, INDEX]).splitlines()
    assert "BEST FIX" not in lines[0] and "UNSAFE" in lines[0] and "do not recommend" in lines[0]
    text = llm.template_explanation(result(UNSAFE, INDEX))
    assert "No safe fix was found" in text and "Best fix" not in text and "SELECT 1" not in text


def test_the_template_names_the_top_pick_with_measured_numbers_first():
    text = llm.template_explanation(result(PARTITION, INDEX))
    assert "Best fix: PARTITION t_1 BY RANGE (c_2) MONTHLY" in text and "CREATE INDEX" not in text
    assert "Measured on a sandbox copy: 62% faster (110 ms -> 42 ms)" in text
    assert "planner's own estimate was 94% lower cost" in text and "1 other option(s)" in text
    plain = llm.template_explanation(result(INDEX))                                  # nothing measured: as before
    assert "planner's estimated cost drops from 1,000 to 820, about 18% lower" in plain and "Measured" not in plain


def test_the_pipeline_hands_the_llm_the_page_top_pick_even_when_ai_lists_another_fix_first():
    """P3 lists a simulated index first; the page's top pick is the measured partition. The prompt that
    is really sent must make the partition the BEST FIX, with its measured numbers."""
    index = rec("index", INDEX_M)                                                      # simulated, listed first
    partition = rec("partition", PART_M, verified_by="timed_run", measured_ms_before=800.0,
                    measured_ms_after=200.0, measured_gain_pct=75.0)
    prompts = []

    def ollama(prompt):
        prompts.append(prompt)
        return "ok", {}
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", ollama), (llm, "start_warmup", lambda: None),
                                 (rl, "recommend", lambda *a, **k: [dict(index), dict(partition)]),
                                 (gnn, "find_bottleneck", stand_in_bottleneck)]:
            stack.enter_context(patched(obj, name, value))
        r = pipeline.analyze("Why is the weekly sales report slow?")
    assert [x["kind"] for x in r["recommendations"]] == ["partition", "index"]
    lines = [x for x in prompts[0].splitlines() if x[:2] in ("1.", "2.")]
    assert lines[0].startswith("1. BEST FIX: [partition]") and "measured 75% faster (800 ms -> 200 ms)" in lines[0]
    assert lines[1].startswith("2. alternative: [index]")


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
