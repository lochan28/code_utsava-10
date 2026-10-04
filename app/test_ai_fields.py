"""Tests for Step 3 of the ai/ update: the dashboard shows the fields Person 3's agent adds.

Run:  .venv/Scripts/python -m app.test_ai_fields      (from the project folder)

measured_gain_pct (headline), measured_ms_before -> measured_ms_after, planner_gain_pct (second, in the
collapsed planner box), timing_note, rejected_fixes, combined fixes (two steps). The 50% target banner stays.
"""
from app import tradeoff
from app.test_before_after import index_result, markers_in_order
from app.test_recommendations import md, page_text, run_page

AI = {"verified_by": "timed_run", "measured_ms_before": 800.0, "measured_ms_after": 200.0,
      "measured_gain_pct": 74.8, "planner_gain_pct": 90.0,
      "timing_note": "median of 5 sandbox runs each, same results"}
REJECTED = ["CREATE INDEX ON t_a3f (c_9) rejected: it returned different results in the sandbox run"]


def with_ai(**extra):
    return index_result(cost=(1000.0, 100.0), time=None, **{**AI, **extra})


def test_resolve_reads_the_top_picks_own_measured_fields():
    n = tradeoff.resolve(with_ai())
    t = n["time"]
    assert (t["before"], t["after"], t["pct"], t["source"]) == (800.0, 200.0, 74.8, "measured")
    assert t["note"] == AI["timing_note"] and t["status"] == "met"
    assert n["cost"]["pct"] == 90.0 and n["cost"]["source"] == "planner estimate"


def test_resolve_uses_ais_gain_but_ignores_junk_and_strings():
    assert tradeoff.resolve(with_ai(measured_gain_pct="75"))["time"]["pct"] == 75.0        # computed from the ms instead
    assert tradeoff.resolve(with_ai(measured_ms_before="x"))["time"] is None
    n = tradeoff.resolve(with_ai(rejected_fixes="oops", timing_note=5))
    assert n["rejected"] == [] and "note" not in n["time"]
    n = tradeoff.resolve(with_ai(rejected_fixes=["a", 3, None, "b", "c", "d", "e", "f"]))
    assert n["rejected"] == ["a", "b", "c", "d", "e"]                                       # strings only, at most 5


def test_page_headline_is_the_measured_gain_and_the_timing_note_is_shown():
    at = run_page(with_ai())
    text = page_text(at)
    assert any("74.8%" in str(m.value) for m in at.metric) and "800 ms → 200 ms" in text
    assert AI["timing_note"] in text
    assert "Meets the target" in text
    assert [e.label for e in at.expander if e.label.startswith("Planner estimate")]         # planner box stays collapsed
    assert "measured improvement" in text and "planner estimate" in text                     # in the evidence list


def test_the_50_percent_banner_stays_when_the_measured_gain_is_below_it():
    at = run_page(with_ai(measured_ms_after=520.0, measured_gain_pct=35.0))
    first = markers_in_order(at)[0]                                    # the HEADLINE marker is the measured one
    assert first[0] == "warning" and first[1].startswith("Below the 50% target"), first


def test_rejected_fixes_show_as_a_neutral_note_and_only_when_there_are_some():
    at = run_page(with_ai(rejected_fixes=REJECTED))
    notes = [i.value for i in at.info if "Dropped by the AI" in i.value]
    assert len(notes) == 1 and "c_9" in notes[0] and "returned different results" in notes[0]
    assert not any("Dropped by the AI" in e.value for e in at.error)                        # not red
    assert not [i for i in run_page(with_ai()).info if "Dropped by the AI" in i.value]


def test_a_combined_fix_shows_both_steps_and_a_badge():
    part, idx = "PARTITION t_a3f BY RANGE (c_9) MONTHLY", "CREATE INDEX ON t_a3f (c_1)"
    at = run_page(with_ai(kind="partition", action=f"{part}; {idx}", combined=True))
    codes = [c.value for c in at.code]
    assert part in codes and idx in codes and f"{part}; {idx}" not in codes
    caps = [c.value for c in at.caption]
    assert "Step 1 of 2" in caps and "Step 2 of 2" in caps
    assert "combined fix" in page_text(at)


NO_FIX_TEXT = "No fix text (the masked SQL could not be analysed)."


def test_no_fix_text_note_shows_only_when_a_fix_has_no_action():
    def notes(**extra):
        return [c.value for c in run_page(with_ai(**extra)).caption if c.value == NO_FIX_TEXT]
    assert notes() == []                                                              # a normal fix: never
    assert notes(rewrite_sql="SELECT 1", rewrite_rule="x") == []                      # a fix with a rewrite: never
    assert notes(kind="partition", action="PARTITION t BY RANGE (d) MONTHLY; CREATE INDEX ON t (a)", combined=True) == []
    assert notes(action="") == [NO_FIX_TEXT] and notes(action=None) == [NO_FIX_TEXT] and notes(action=7) == [NO_FIX_TEXT]
    at = run_page(with_ai(action="", rewrite_sql="SELECT 1", rewrite_rule="x"))        # no action but a rewrite: both show
    assert [c.value for c in at.caption].count(NO_FIX_TEXT) == 1 and "SELECT 1" in [c.value for c in at.code]


def test_the_page_has_no_real_name_in_any_of_the_new_parts():
    text = page_text(run_page(with_ai(rejected_fixes=REJECTED))).lower()
    assert "rahul" not in text and "customers" not in text


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
