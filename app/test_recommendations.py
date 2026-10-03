"""Tests for Step 4b: bottleneck card, recommendations table, honesty badges.

Run:  .venv/Scripts/python -m app.test_recommendations      (from the project folder)

Helpers are tested directly; the page is tested headlessly with Streamlit's AppTest, using
a FAKE pipeline.analyze so every case (full data, missing keys, unsafe rewrite...) is exact.
Ollama is switched off. The decoder (mask.unmask) is rigged to explode if the page calls it.
"""
import copy
from contextlib import ExitStack
from pathlib import Path

from streamlit.testing.v1 import AppTest

import pipeline
from app import llm, sample_data
from app import ui_helpers as ui
from app.test_support import AI_STAND_INS
from app.test_privacy import fresh_audit_log, patched
from privacy import mask

DASHBOARD = str(Path(__file__).with_name("dashboard.py"))
QUESTION = "Why is the weekly sales report slow?"
TOP_PICK_BADGE = ":green-badge[:material/star: top pick]"      # the badge itself, not the words


def _no_ollama(prompt):
    raise llm.LLMUnavailable("Ollama is off in this test")


def _decoder_must_not_run(*args, **kwargs):
    raise AssertionError("the dashboard called the decoder (mask.unmask)")


def run_page(result, question=QUESTION, *more_patches):
    """Open the dashboard, ask one question, and make analyze() return `result`."""
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 (pipeline, "analyze", lambda q: result),
                                 (mask, "unmask", _decoder_must_not_run), *more_patches]:
            stack.enter_context(patched(obj, name, value))
        at = AppTest.from_file(DASHBOARD, default_timeout=30).run()
        at.chat_input[0].set_value(question).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def full(**changes):
    """A complete sample result, with some keys replaced."""
    return {**copy.deepcopy(sample_data.demo_result()), **changes}


def md(at):
    return [m.value for m in at.markdown]


def page_text(at, skip_user_bubble=True):
    """EVERY piece of text on the page (markdown, captions, code, warnings, expander
    labels, json...), optionally leaving out the admin's own question bubble."""
    out = []

    def walk(block):
        for child in block.children.values():
            if skip_user_bubble and getattr(child, "type", "") == "chat_message" and child.name == "user":
                continue
            for attr in ("value", "label", "body"):
                v = getattr(child, attr, None)
                if isinstance(v, str):
                    out.append(v)
            if hasattr(child, "children"):
                walk(child)
    walk(at.main)
    return "\n".join(out)


# =================================================================== helpers
def test_each_verified_by_maps_to_the_right_badge():
    assert ui.verified_badge({"verified_by": "timed_run"}) == ("measured", "green")
    assert ui.verified_badge({"verified_by": "hypopg_cost"}) == ("cost-simulated", "blue")
    assert ui.verified_badge({"verified_by": "rule_only"}) == ("rule-based, not simulated", "gray")
    for odd in ({}, {"verified_by": None}, {"verified_by": "weird"}, {"verified_by": 123},
                {"verified_by": ["timed_run"]}, {"verified_by": ""}):
        assert ui.verified_badge(odd) == ("unverified", "gray"), odd
    # an unsafe rewrite shows ONLY the red badge, even if it was "measured"
    for unsafe in ({"verified_by": "timed_run", "unsafe_rewrite": True},
                   {"verified_by": "timed_run", "same_results": False},
                   {"verified_by": "hypopg_cost", "unsafe_rewrite": True}):
        assert ui.verified_badge(unsafe) == ("UNSAFE: results differ", "red"), unsafe
    assert ui.verified_badge({"verified_by": "timed_run", "unsafe_rewrite": False, "same_results": True}) == ("measured", "green")


def test_novelty_badge_only_for_unseen_shapes():
    assert ui.novelty_badge({"novelty": "unseen shape (lower confidence)"}) == ("unseen query shape", "orange")
    assert ui.novelty_badge({"novelty": "seen query shape"}) is None
    assert ui.novelty_badge({}) is None and ui.novelty_badge({"novelty": 5}) is None


def test_sort_measured_simulated_rule_only_unverified_unsafe_last():
    recs = [{"id": "u1", "unsafe_rewrite": True}, {"id": "r1", "verified_by": "rule_only"},
            {"id": "b1"}, {"id": "s1", "verified_by": "hypopg_cost"}, {"id": "m1", "verified_by": "timed_run"},
            {"id": "s2", "verified_by": "hypopg_cost"}, {"id": "m2", "verified_by": "timed_run"},
            {"id": "u2", "verified_by": "timed_run", "same_results": False}]
    got = [r["id"] for r in ui.sort_recommendations(recs)]
    print(f"      sorted: {got}")
    assert got == ["m1", "m2", "s1", "s2", "r1", "b1", "u1", "u2"]      # stable inside each tier
    assert ui.sort_recommendations([None, 5, "x", {"id": "ok"}]) == [{"id": "ok"}]
    assert ui.sort_recommendations(None) == ui.sort_recommendations("nope") == ui.sort_recommendations([]) == []


def test_confidence_strip_counts():
    recs = sample_data.SAMPLE_RECOMMENDATIONS
    assert ui.confidence_strip(ui.confidence_counts(recs)) == \
        "1 measured · 1 simulated · 1 rule-only · 1 unverified · 1 unsafe"
    assert ui.confidence_strip(ui.confidence_counts([])) == "0 measured · 0 simulated · 0 rule-only · 0 unsafe"


def test_evidence_items_and_none_means_no_evidence():
    assert ui.evidence_items({}) == [] and ui.evidence_items({"kind": "index", "action": "x"}) == []
    assert ui.evidence_items({"cost_before": "n/a", "cost_after": None}) == []
    got = dict(ui.evidence_items({"cost_before": 19437.0, "cost_after": 812.0, "improvement_pct": 95.8,
                                  "time_before_ms": 418.0, "time_after_ms": 118.0, "speedup": 3.54,
                                  "verify_note": "same 108 rows"}))
    assert got["planner cost"] == "19,437 → 812" and got["measured time"] == "418 ms → 118 ms"
    assert got["speedup"] == "3.5x" and got["check"] == "same 108 rows" and got["estimated improvement"] == "96%"
    assert ui.evidence_items({"evidence": {"table": "t_a3f", "empty": None}}) == [("table", "t_a3f")]


def test_bottleneck_items_show_what_exists_and_skip_the_rest():
    assert dict(ui.bottleneck_items(sample_data.SAMPLE_BOTTLENECK)) == {
        "step": "Seq Scan", "table": "t_a3f", "share of plan cost": "92%", "estimated rows": "1,200,000",
        "found by": "rule-based check",
        "why": "A full scan of t_a3f reads 1,200,000 rows and is 92% of the plan's cost."}
    stub = {"node_id": 0, "op": "Seq Scan", "reason": "Full scan.", "evidence": {"table": "t_a3f", "cost_share": 0.92}}
    assert dict(ui.bottleneck_items(stub)) == {"step": "Seq Scan", "share of plan cost": "92%", "why": "Full scan."}
    assert ui.bottleneck_items({"path": "gnn"}) == [("found by", "GNN")]
    for junk in (None, {}, [], "text", 5, {"op": None, "evidence": "x", "rows": "many"}):
        assert ui.bottleneck_items(junk) == [], junk
    assert ui.bottleneck_node_id({"node_id": 2}) == 2
    assert all(ui.bottleneck_node_id(x) is None for x in (None, {}, {"node_id": -1}, {"node_id": "1"}, {"node_id": True}))


def test_plan_tree_marks_only_the_bottleneck_step():
    plan = sample_data.SAMPLE_PLAN
    marked = [l for l in ui.plan_tree_text(plan, highlight=1).splitlines() if "BOTTLENECK" in l]
    assert len(marked) == 1 and "Seq Scan on t_a3f" in marked[0]
    for no_mark in (None, 99, -1, True, "1"):
        assert "BOTTLENECK" not in ui.plan_tree_text(plan, highlight=no_mark), no_mark


def test_dollar_signs_are_escaped_for_markdown():
    assert ui.md_safe("WHERE c_1 = $1 AND c_2 = $2") == "WHERE c_1 = \\$1 AND c_2 = \\$2"


def test_ai_inputs_line_shows_only_known_status_words():
    ok = {"time_fn": "used", "stats": "not available yet", "column_types": "dropped: failed the leak check"}
    assert ui.ai_inputs_line(ok) == ("Inputs given to the AI → time_fn: used · stats: not available yet · "
                                     "column_types: dropped: failed the leak check")
    line = ui.ai_inputs_line({"time_fn": "used", "stats": "orders.customer_email = rahul@x.com", "extra": "used"})
    assert line == "Inputs given to the AI → time_fn: used" and "rahul" not in line
    assert ui.ai_inputs_line(None) == ui.ai_inputs_line({}) == ui.ai_inputs_line("x") == ""


def test_the_pipelines_real_ai_inputs_hold_only_status_words():
    """ai_inputs is built by pipeline.py: check it carries no data, only the fixed vocabulary."""
    with fresh_audit_log(), patched(llm, "_call_ollama", _no_ollama), patched(llm, "start_warmup", lambda: None):
        r = pipeline.analyze(QUESTION)
    print(f"      real ai_inputs: {r['ai_inputs']}")
    assert set(r["ai_inputs"]) <= set(ui.AI_INPUT_KEYS)
    assert all(v in ui.AI_INPUT_STATUSES for v in r["ai_inputs"].values())


# =================================================================== the page
def test_page_full_data_shows_every_badge_and_the_strip():
    at = run_page(full())
    m = md(at)
    print(f"      badges: {[v for v in m if '-badge[' in v and 'LLM' not in v and 'raw values' not in v]}")
    for badge in (":green-badge[measured]", ":blue-badge[cost-simulated]", ":gray-badge[rule-based, not simulated]",
                  ":gray-badge[unverified]", ":red-badge[UNSAFE: results differ]", ":orange-badge[unseen query shape]"):
        assert badge in m, badge
    assert "**Confidence:** 1 measured · 1 simulated · 1 rule-only · 1 unverified · 1 unsafe" in m
    assert any(v.startswith("#### Seq Scan on t_a3f") for v in m)                 # bottleneck card
    assert "share of plan cost: **92%**  ·  estimated rows: **1,200,000**" in m
    assert ":gray-badge[:material/search: found by: rule-based check]" in m
    assert any("Masked view" in c.value for c in at.caption)                      # the masked-view label
    assert [e.label for e in at.expander] == ["Masked execution plan", "Summary and why not the alternatives",
                                              "Planner estimate (can disagree with measured time)"]
    tree = [c.value for c in at.code if "<== BOTTLENECK" in c.value]
    assert len(tree) == 1 and "Seq Scan on t_a3f" in tree[0].splitlines()[1]      # the right step is marked
    assert any("Summary:" in v for v in m) and any("Why not the alternative:" in v for v in m)


def test_page_sorts_unsafe_last_and_never_calls_it_top_pick():
    at = run_page(full())
    m = md(at)
    pos = {name: next(i for i, v in enumerate(m) if v == badge) for name, badge in {
        "measured": ":green-badge[measured]", "simulated": ":blue-badge[cost-simulated]",
        "rule": ":gray-badge[rule-based, not simulated]", "unverified": ":gray-badge[unverified]",
        "unsafe": ":red-badge[UNSAFE: results differ]"}.items()}
    print(f"      badge positions on the page: {pos}")
    assert pos["measured"] < pos["simulated"] < pos["rule"] < pos["unverified"] < pos["unsafe"]
    tops = [i for i, v in enumerate(m) if v == TOP_PICK_BADGE]
    assert len(tops) == 1 and tops[0] < pos["simulated"], "top pick must be the measured fix"
    actions = [c.value for c in at.code if c.language == "sql"][1:]               # [0] = the masked query
    assert actions[0].startswith("SELECT t_b22.c_1") and actions[-1].startswith("SELECT c_1, COUNT(*)")
    assert len(at.error) == 1 and at.error[0].value.startswith("**UNSAFE: results differ.**")


def test_page_unsafe_rewrite_shows_red_and_not_a_green_measured_badge():
    unsafe_only = [r for r in sample_data.SAMPLE_RECOMMENDATIONS if r.get("unsafe_rewrite")]
    at = run_page(full(recommendations=unsafe_only))
    m = md(at)
    assert ":red-badge[UNSAFE: results differ]" in m
    assert ":green-badge[measured]" not in m, "an unsafe rewrite also showed the green 'measured' badge"
    assert TOP_PICK_BADGE not in m, "an unsafe rewrite was labelled top pick"
    assert any("No safe fix was found" in e.value for e in at.error)


def test_page_no_evidence_gets_a_visible_warning():
    bare = [{"kind": "index", "action": "CREATE INDEX ON t_a3f (c_1)", "reason": "r"},
            {"kind": "index", "verified_by": "hypopg_cost", "action": "CREATE INDEX ON t_a3f (c_2)",
             "reason": "r", "cost_before": 100.0, "cost_after": 10.0}]
    at = run_page(full(recommendations=bare))
    warnings = [w.value for w in at.warning]
    print(f"      warnings: {warnings}")
    assert warnings == ["No evidence attached"], "exactly the evidence-less row must warn"


def test_page_never_crashes_on_missing_or_odd_data():
    at = run_page({"leak_check": True})                                  # almost nothing at all
    assert any(c.value == "No bottleneck details available." for c in at.caption)
    assert any("No recommendations were returned" in i.value for i in at.info)
    for recs in ([], None, "not a list", 42, [None, 5, "x"]):
        at = run_page(full(recommendations=recs, bottleneck=None))
        assert any("No recommendations were returned" in i.value for i in at.info), recs
    at = run_page(full(recommendations=[{}, {"kind": None, "action": 5, "reason": 7, "evidence": "x"}],
                       bottleneck={"op": None, "node_id": "x", "evidence": 1}, masked_plan="weird", ai_inputs=7))
    assert md(at).count(":gray-badge[unverified]") == 2 and len(at.warning) == 2     # shown, flagged, no crash
    at = run_page(full(recommendations=[{"kind": "none", "action": "", "verified_by": "rule_only",
                                         "cost_before": 5.0, "cost_after": 5.0}]))
    assert any("No fix text" in c.value for c in at.caption)


def test_page_has_no_real_name_anywhere_and_never_calls_the_decoder():
    """The Rahul check on ALL page text. (The admin's own question bubble is the one allowed place.)"""
    question = "why is Rahul Sharma's page slow"
    at = run_page(full(), question)                       # run_page rigs mask.unmask to explode if called
    everything = page_text(at).lower()
    assert "rahul" not in everything and "sharma" not in everything
    assert "rahul" in page_text(at, skip_user_bubble=False).lower()      # ...but it IS in the user's own bubble
    assert at.chat_message[0].name == "user"
    # the un-masked fix must not be on the page either
    assert "orders" not in everything and "create index on orders" not in everything
    assert not any("deploy" in c.value.lower() for c in at.code)


def test_page_with_the_real_pipeline_stubs_end_to_end():
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", _no_ollama), (llm, "start_warmup", lambda: None),
                                 *AI_STAND_INS]:
            stack.enter_context(patched(obj, name, value))
        at = AppTest.from_file(DASHBOARD, default_timeout=30).run()
        at.chat_input[0].set_value(QUESTION).run()
    assert not at.exception
    m = md(at)
    assert any(v.startswith("#### Seq Scan") for v in m)                # stub bottleneck: only op/reason/evidence
    assert ":gray-badge[unverified]" in m                               # stub rec has no verified_by
    assert "**Confidence:** 0 measured · 0 simulated · 0 rule-only · 1 unverified · 0 unsafe" in m
    assert at.warning == [] and "orders" not in page_text(at).lower()   # stub rec has cost numbers = evidence


def test_page_escapes_dollar_placeholders_in_text():
    rec = {"kind": "rewrite", "verified_by": "timed_run", "same_results": True, "action": "SELECT $1",
           "reason": "Compare $1 with $2 once.", "cost_before": 2.0, "cost_after": 1.0}
    at = run_page(full(recommendations=[rec]))
    assert any("Compare \\$1 with \\$2 once." == v for v in md(at))      # no maths-formula garbling
    assert any(c.value == "SELECT $1" for c in at.code)                  # code blocks keep it as is


def test_page_shows_ai_inputs_status_words_only():
    at = run_page(full(ai_inputs={"time_fn": "used", "stats": "orders.email=rahul@x.com", "column_types": "unavailable"}))
    caps = [c.value for c in at.caption if c.value.startswith("Inputs given to the AI")]
    assert caps == ["Inputs given to the AI → time_fn: used · column_types: unavailable"]
    assert "rahul" not in page_text(at).lower()


def test_sample_toggle_shows_every_badge_type_with_a_loud_warning():
    with ExitStack() as stack:
        for obj, name, value in [(llm, "start_warmup", lambda: None), (mask, "unmask", _decoder_must_not_run)]:
            stack.enter_context(patched(obj, name, value))
        at = AppTest.from_file(DASHBOARD, default_timeout=30).run()
        at.sidebar.toggle[0].set_value(True).run()
    assert not at.exception
    assert any(w.value.startswith("SAMPLE DATA") for w in at.warning)
    m = md(at)
    for badge in (":green-badge[measured]", ":blue-badge[cost-simulated]", ":gray-badge[rule-based, not simulated]",
                  ":gray-badge[unverified]", ":red-badge[UNSAFE: results differ]"):
        assert badge in m, badge


def sample_shown(at):
    return any(w.value.startswith("SAMPLE DATA") for w in at.warning)


def open_page(analyze_returns=None, **state):
    """Open the dashboard (Ollama off, decoder rigged to explode) with some session state preset.
    analyze_returns: the result a question gets (the real pipeline would call the decoder itself)."""
    with ExitStack() as stack:
        for obj, name, value in [(llm, "start_warmup", lambda: None), (llm, "_call_ollama", _no_ollama),
                                 (mask, "unmask", _decoder_must_not_run),
                                 (pipeline, "analyze", lambda q: analyze_returns)]:
            stack.enter_context(patched(obj, name, value))
        at = AppTest.from_file(DASHBOARD, default_timeout=30)
        for k, v in state.items():
            at.session_state[k] = v
        at.run()
        return at, stack.pop_all()           # keep the patches alive for further .run() calls


def test_sample_toggle_is_off_by_default():
    with fresh_audit_log(), patched(llm, "_call_ollama", _no_ollama), patched(llm, "start_warmup", lambda: None):
        canned = pipeline.analyze(QUESTION)                    # a real (stub) result, no sample data in it
    at, patches = open_page(analyze_returns=canned)
    with patches:
        assert at.sidebar.toggle[0].value is False and not sample_shown(at)
        at.chat_input[0].set_value(QUESTION).run()            # a real question does not switch it on
        assert not at.exception
        assert at.sidebar.toggle[0].value is False and not sample_shown(at)
        assert ":red-badge[UNSAFE: results differ]" not in md(at)     # nothing sample-like on the page
        at.sidebar.toggle[0].set_value(True).run()            # only the person switching it on shows it
        assert sample_shown(at) and ":red-badge[UNSAFE: results differ]" in md(at)


def test_sample_data_can_never_show_while_demo_mode_is_on():
    # 1) demo mode on from the start, even with the sample switch forced on
    at, patches = open_page(demo_mode=True, show_sample=True)
    with patches:
        assert not at.exception and not sample_shown(at)
        assert ":red-badge[UNSAFE: results differ]" not in md(at)
        assert at.sidebar.toggle[0].disabled is True                      # the switch is greyed out
    # 2) demo mode switched on WHILE the sample is on screen: it disappears at once
    at, patches = open_page()
    with patches:
        at.sidebar.toggle[0].set_value(True).run()
        assert sample_shown(at)
        at.session_state["demo_mode"] = True
        at.run()
        assert not at.exception and not sample_shown(at), "sample data stayed visible in demo mode"
        assert ":red-badge[UNSAFE: results differ]" not in md(at)
        # 3) and back off again: the switch is usable, sample not shown until asked for
        at.session_state["demo_mode"] = False
        at.session_state["show_sample"] = False
        at.run()
        assert at.sidebar.toggle[0].disabled is False and not sample_shown(at)


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
