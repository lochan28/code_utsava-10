"""Tests for Step 4c: before/after charts, the 50% target, write latency, storage, trade-off.

Run:  .venv/Scripts/python -m app.test_before_after      (from the project folder)

The number rules are tested directly; the page is tested headlessly with a FAKE
pipeline.analyze so each case (no measured time, missing storage, a worse fix...) is exact.
Charts are read back from what Streamlit really sends: the axis title and the plotted bars.
"""
import json
import math

import pyarrow as pa

from app import charts, sample_data, tradeoff
from app.test_recommendations import QUESTION, full, md, open_page, page_text, run_page
from app.test_privacy import fresh_audit_log  # noqa: F401  (kept for parity with the other suites)


# =================================================================== helpers
def index_result(cost=(19437.0, 812.0), time=(418.0, 118.0), write=0.002, storage=22_000_000, **rec_extra):
    """A result whose top fix is an INDEX (it has a write and storage price). Pass None to drop a number."""
    rec = {"kind": "index", "verified_by": "hypopg_cost", "action": "CREATE INDEX ON t_a3f (c_1)",
           "reason": "No index.", **rec_extra}
    if cost:
        rec["cost_before"], rec["cost_after"] = cost
    r = {"leak_check": True, "masked_query": "SELECT 1", "masked_plan": sample_data.SAMPLE_PLAN,
         "bottleneck": sample_data.SAMPLE_BOTTLENECK, "recommendations": [rec]}
    if time:
        r["time_before_ms"], r["time_after_ms"] = time
    if write is not None:
        r["write_latency_ms"] = write
    if storage is not None:
        r["storage_bytes"] = storage
    return r


def elements(at):
    out = []

    def walk(block):
        for child in block.children.values():
            out.append(child)
            if hasattr(child, "children"):
                walk(child)
    walk(at.main)
    return out


def charts_of(at):
    """Every chart on the page, as the browser would get it: axis title, bars, colour of 'after'."""
    found = []
    for el in elements(at):
        if getattr(el, "type", "") == "vega_lite_chart":
            spec = json.loads(el.proto.spec)
            layer = spec["layer"][0]
            rows = pa.ipc.open_stream(el.proto.data.data).read_all().to_pylist()
            found.append({"y_title": layer["encoding"]["y"]["title"],
                          "bars": [(r["when"], r["value"]) for r in rows],
                          "after_color": layer["encoding"]["color"]["scale"]["range"][1]})
    return found


def metrics_of(at):
    return {m.label: m.value for m in at.metric}


def captions(at):
    return [c.value for c in at.caption]


def verdicts(at):
    """(success texts, warning texts, error texts), leaving out the unrelated ones."""
    skip = ("No evidence attached", "SAMPLE DATA")
    return ([s.value for s in at.success],
            [w.value for w in at.warning if not w.value.startswith(skip)],
            [e.value for e in at.error])


# =================================================================== the number rules
def test_improvement_math_and_bad_input():
    assert tradeoff.improvement(200, 100) == 50.0
    assert round(tradeoff.improvement(19437, 812), 1) == 95.8
    assert math.isclose(tradeoff.improvement(100, 120), -20.0)       # worse is negative (float: not exact)
    assert tradeoff.improvement(100, 0) == 100.0
    for bad in [(0, 5), (-5, 1), (None, 3), ("x", 1), (100, True), (100, float("nan")), (100, -5),
                (float("inf"), 1), (100, None), ([1], 2)]:
        assert tradeoff.improvement(*bad) is None, bad


def test_target_flips_at_exactly_50_percent():
    assert tradeoff.TARGET_PCT == 50.0
    assert tradeoff.target_status(50.0) == "below"                   # "more than 50%": 50 itself is not enough
    assert tradeoff.target_status(50.0000001) == "met"
    assert tradeoff.target_status(49.9999) == "below"
    assert tradeoff.target_status(100.0) == "met"
    assert tradeoff.target_status(0.0) == "none"
    assert tradeoff.target_status(0.0001) == "below"
    assert tradeoff.target_status(-0.0001) == "worse"
    for odd in (None, float("nan"), "50", True, [50]):
        assert tradeoff.target_status(odd) == "unknown", odd
    assert tradeoff.target_status(tradeoff.improvement(200, 100)) == "below"        # exactly 50.0
    assert tradeoff.target_status(tradeoff.improvement(200, 99.99)) == "met"


def test_resolve_index_top_uses_its_own_and_the_results_numbers():
    n = tradeoff.resolve(index_result())
    assert n["cost"]["source"] == "planner estimate" and n["cost"]["status"] == "met"
    assert (n["cost"]["before"], n["cost"]["after"]) == (19437.0, 812.0)
    assert n["time"]["source"] == "measured" and (n["time"]["before"], n["time"]["after"]) == (418.0, 118.0)
    assert (n["write"], n["storage"], n["write_note"]) == (0.002, 22_000_000, None)


def test_resolve_rewrite_top_has_no_write_or_storage_price_by_design():
    n = tradeoff.resolve(full())
    assert n["top"]["kind"] == "rewrite" and n["write"] is None and n["storage"] is None
    assert n["write_note"] == n["storage_note"] == tradeoff.NO_INDEX_NOTE
    assert n["time"]["source"] == "measured" and (n["time"]["before"], n["time"]["after"]) == (418.0, 118.0)


def test_resolve_never_lends_one_fix_the_numbers_of_another():
    partition = {"kind": "partition", "verified_by": "rule_only", "action": "PARTITION t_a3f BY RANGE (c_1) MONTHLY"}
    r = index_result()
    r["recommendations"] = [partition, r["recommendations"][0]]      # the index is on top but NOT listed first
    n = tradeoff.resolve(r)
    assert n["top"]["kind"] == "index"
    assert n["time"] is None and n["write"] is None and n["storage"] is None, n   # r-level numbers are the partition's
    assert n["cost"] is not None                                      # the index's own cost numbers are fine


def test_resolve_edge_cases():
    unsafe = {"kind": "rewrite", "unsafe_rewrite": True, "cost_before": 10.0, "cost_after": 1.0}
    n = tradeoff.resolve({"recommendations": [unsafe], "cost_before": 10.0, "cost_after": 1.0})
    assert n["no_safe"] is True and n["cost"] is None and n["time"] is None      # no good news from an unsafe fix
    n = tradeoff.resolve({"cost_before": 100.0, "cost_after": 40.0})             # no recommendations at all
    assert n["no_safe"] is False and n["cost"]["pct"] == 60.0
    for nothing in ({}, {"recommendations": None}, {"recommendations": [None, 5]}):
        n = tradeoff.resolve(nothing)
        assert (n["cost"], n["time"], n["write"], n["storage"], n["no_safe"]) == (None, None, None, None, False)
    assert tradeoff.resolve(index_result(time=None))["time"] is None             # no measured time -> None, not 0


def test_formatters_never_turn_missing_into_zero():
    assert tradeoff.fmt_write(0.002) == "+0.002 ms per insert"
    assert tradeoff.fmt_write(0.0) == "+0 ms per insert"             # a real measured zero is a number
    assert tradeoff.fmt_write(-0.01) == "-0.01 ms per insert"
    assert tradeoff.fmt_storage(22_000_000) == "+21.0 MB"
    assert tradeoff.fmt_storage(0) == "+0.0 MB"
    for missing in (None, "x", True, float("nan")):
        assert tradeoff.fmt_write(missing) == tradeoff.fmt_storage(missing) == tradeoff.NOT_MEASURED == "not measured"
    assert tradeoff.fmt_ms(418.0) == "418 ms" and tradeoff.fmt_ms(3.04) == "3.0 ms"
    assert tradeoff.fmt_pct(95.83) == "95.8%" and tradeoff.fmt_pct(None) == "not measured"


def test_chart_builder_labels_the_axis_and_colours_by_status():
    spec = charts.before_after_chart(19437, 812, "planner cost units (estimate)", status="worse").to_dict()
    assert spec["layer"][0]["encoding"]["y"]["title"] == "planner cost units (estimate)"
    assert [d["value"] for d in spec["data"]["values"]] == [19437.0, 812.0]
    colours = {s: charts.before_after_chart(1, 1, "u", status=s).to_dict()["layer"][0]["encoding"]["color"]["scale"]["range"][1]
               for s in charts.STATUS_COLORS}
    assert len(set(colours[s] for s in ("met", "below", "worse"))) == 3, colours   # green / amber / red differ
    assert colours["worse"] == "#d64545"


# =================================================================== the page
def test_page_full_data_charts_numbers_and_sources():
    at = run_page(index_result())
    cs = charts_of(at)
    print(f"      charts: {[(c['y_title'], c['bars']) for c in cs]}")
    print(f"      metrics: {metrics_of(at)}")
    assert metrics_of(at) == {"Estimated cost improvement": "95.8%", "Measured time improvement": "71.8%",
                              "Write latency": "+0.002 ms per insert", "Extra storage": "+21.0 MB"}
    assert [(c["y_title"], c["bars"]) for c in cs] == [          # the measured time leads, the estimate follows
        ("measured milliseconds", [("before", 418.0), ("after", 118.0)]),
        ("planner cost units (estimate)", [("before", 19437.0), ("after", 812.0)])]
    caps = captions(at)
    for source in ("Source: planner estimate", "418 ms → 118 ms · Source: measured",
                   "Source: measured on a sandbox copy"):
        assert source in caps, source
    assert caps.count("Source: measured on a sandbox copy") == 2      # write latency AND storage
    ok_, warn, err = verdicts(at)
    assert len(ok_) == 2 and all("Meets the target: more than 50% improvement." == s for s in ok_) and not warn and not err
    text = page_text(at)
    assert "You gain" in text and "71.8% faster (measured)" in text           # only the measured gain is the headline
    assert "lower estimated cost" not in text
    assert "You pay in writes" in text and "You pay in storage" in text and "+21.0 MB" in text


def test_page_never_puts_cost_units_and_milliseconds_on_one_chart():
    cs = charts_of(run_page(index_result()))
    assert len(cs) == 2 and all(len(c["bars"]) == 2 for c in cs)
    time_chart, cost_chart = cs                       # the measured time is the headline, so it is first
    assert "cost" in cost_chart["y_title"] and "millisecond" not in cost_chart["y_title"]
    assert "millisecond" in time_chart["y_title"] and "cost" not in time_chart["y_title"]
    assert cost_chart["y_title"] != time_chart["y_title"]
    assert not {v for _, v in cost_chart["bars"]} & {v for _, v in time_chart["bars"]}      # different numbers


def test_page_without_measured_time_says_so_instead_of_an_empty_chart():
    at = run_page(index_result(time=None))
    assert any(i.value == "Not measured, cost estimate only." for i in at.info)
    cs = charts_of(at)
    assert [c["y_title"] for c in cs] == ["planner cost units (estimate)"]                  # only the cost chart
    assert "Measured time improvement" not in metrics_of(at)
    assert not any("· Source: measured" in c for c in captions(at))
    assert "faster (measured)" not in page_text(at)


def test_page_missing_storage_or_write_shows_not_measured_never_zero():
    at = run_page(index_result(storage=None))
    m = metrics_of(at)
    print(f"      storage missing -> {m['Extra storage']!r}, write still {m['Write latency']!r}")
    assert m["Extra storage"] == "not measured" and m["Write latency"] == "+0.002 ms per insert"
    assert "Source: not available" in captions(at)
    assert "+0.0 MB" not in page_text(at)
    m = metrics_of(run_page(index_result(write=None)))
    assert m["Write latency"] == "not measured" and m["Extra storage"] == "+21.0 MB"
    m = metrics_of(run_page(index_result(write=None, storage=None)))
    assert m["Write latency"] == m["Extra storage"] == "not measured"
    assert metrics_of(run_page(index_result(write=0.0)))["Write latency"] == "+0 ms per insert"   # real zero stays a number


def test_page_rewrite_top_shows_no_price_with_the_reason():
    at = run_page(full())
    assert metrics_of(at)["Write latency"] == metrics_of(at)["Extra storage"] == "none"
    assert captions(at).count(tradeoff.NO_INDEX_NOTE) == 2
    assert "none: a rewrite creates no index" in page_text(at)


def test_page_does_not_lend_another_fix_its_numbers():
    partition = {"kind": "partition", "verified_by": "rule_only", "action": "PARTITION t_a3f BY RANGE (c_1) MONTHLY"}
    r = index_result()
    r["recommendations"] = [partition, r["recommendations"][0]]
    at = run_page(r)
    m = metrics_of(at)
    assert m["Write latency"] == m["Extra storage"] == "not measured"
    assert any(i.value == "Not measured, cost estimate only." for i in at.info)


def test_page_zero_improvement_is_shown_honestly_not_celebrated():
    at = run_page(index_result(cost=(100.0, 100.0), time=None))
    ok_, warn, err = verdicts(at)
    assert metrics_of(at)["Estimated cost improvement"] == "0.0%"
    assert ok_ == [] and warn == ["No improvement."] and err == []
    assert charts_of(at)[0]["after_color"] == "#8a8f98"  # literal gray, not read from the table under test
    assert "no change in estimated cost" in page_text(at)


def test_page_a_fix_that_makes_things_worse_is_red():
    at = run_page(index_result(cost=(1000.0, 1300.0), time=(100.0, 140.0)))
    ok_, warn, err = verdicts(at)
    print(f"      errors: {err}")
    assert ok_ == [] and warn == []
    assert err == ["WORSE: this fix made the query SLOWER when measured.",          # the headline
                   "WORSE: this fix is estimated to make the query MORE expensive."]
    assert metrics_of(at)["Estimated cost improvement"] == "-30.0%" and metrics_of(at)["Measured time improvement"] == "-40.0%"
    assert [c["after_color"] for c in charts_of(at)] == ["#d64545"] * 2  # literal red
    text = page_text(at)
    assert ":red[40.0% SLOWER (measured, worse)]" in text and "HIGHER estimated cost" not in text   # measured only
    assert "Meets the target" not in text and "lower estimated cost" not in text


def test_page_target_marker_flips_at_exactly_50_percent():
    below = verdicts(run_page(index_result(cost=(200.0, 100.0), time=None)))          # exactly 50.0%
    assert below[0] == [] and below[1] == ["Below the 50% target."]
    just_over = verdicts(run_page(index_result(cost=(200.0, 99.9), time=None)))       # 50.05%
    assert just_over[0] == ["Meets the target: more than 50% improvement."] and just_over[1] == []
    t_below = verdicts(run_page(index_result(time=(100.0, 50.0))))                    # time exactly 50%
    assert len(t_below[0]) == 1 and t_below[1] == ["Below the 50% target."]           # cost met, time not
    t_over = verdicts(run_page(index_result(time=(100.0, 49.9))))
    assert len(t_over[0]) == 2 and t_over[1] == []
    assert charts_of(run_page(index_result(cost=(200.0, 100.0), time=None)))[0]["after_color"] == "#e0a030"  # literal amber


def test_page_only_unsafe_fixes_shows_a_warning_and_no_good_news():
    unsafe = [r for r in sample_data.SAMPLE_RECOMMENDATIONS if r.get("unsafe_rewrite")]
    at = run_page(full(recommendations=unsafe, cost_before=100.0, cost_after=1.0, time_before_ms=100.0,
                       time_after_ms=1.0, write_latency_ms=0.1, storage_bytes=1_000_000))
    ok_, warn, err = verdicts(at)
    assert charts_of(at) == [] and ok_ == []
    assert "No improvement is shown: every suggested fix returns different results." in err
    assert not any(k in metrics_of(at) for k in ("Estimated cost improvement", "Measured time improvement",
                                                 "Write latency", "Extra storage"))
    assert "You gain" not in page_text(at) and "Meets the target" not in page_text(at)


def test_page_with_missing_keys_does_not_crash_and_says_not_measured():
    at = run_page({"leak_check": True})
    assert charts_of(at) == []
    assert any(i.value.startswith("Cost: not measured") for i in at.info)
    assert any(i.value == "Not measured, cost estimate only." for i in at.info)
    assert metrics_of(at) == {"Write latency": "not measured", "Extra storage": "not measured"}
    assert "not measured" in page_text(at)


def test_page_with_odd_numbers_does_not_crash():
    nan = float("nan")
    for kwargs in ({"cost": ("x", None)}, {"cost": (True, 3.0)}, {"cost": (nan, nan)}, {"cost": (0.0, 5.0)},
                   {"time": (nan, 5.0)}, {"time": ("a", "b")}, {"write": nan, "storage": True},
                   {"write": "fast", "storage": "big"}, {"cost": (-5.0, 3.0), "time": (0.0, 0.0)}):
        at = run_page(index_result(**kwargs))
        assert not at.exception, kwargs
    at = run_page(index_result(write=nan, storage=True))
    assert metrics_of(at)["Write latency"] == metrics_of(at)["Extra storage"] == "not measured"
    assert charts_of(run_page(index_result(cost=("x", None)))) [0]["y_title"] == "measured milliseconds"   # only time is left


def test_page_has_no_real_name_anywhere_in_the_4c_panels():
    at = run_page(index_result(), "why is Rahul Sharma's page slow")        # run_page rigs the decoder to explode
    text = page_text(at).lower()
    assert "rahul" not in text and "sharma" not in text and "orders" not in text
    assert "rahul" in page_text(at, skip_user_bubble=False).lower()          # only the admin's own bubble has it
    assert all("rahul" not in json.dumps(c).lower() for c in charts_of(at))


def test_sample_variants_each_show_the_right_state():
    at, patches = open_page()
    with patches:
        at.sidebar.toggle(key="show_sample").set_value(True).run()
        assert at.sidebar.radio[0].value == sample_data.VARIANTS[0]          # the normal one is the default
        expect = {}
        for variant in sample_data.VARIANTS:
            at.sidebar.radio[0].set_value(variant).run()
            assert not at.exception, variant
            ok_, warn, err = verdicts(at)
            expect[variant] = (len(charts_of(at)), len(ok_), sum(e.startswith("WORSE") for e in err))
        print(f"      (charts, green markers, WORSE markers) per variant: {expect}")
        assert expect[sample_data.VARIANTS[0]] == (2, 2, 0)                    # rewrite on top: both met
        assert expect[sample_data.VARIANTS[1]] == (2, 2, 0)
        assert expect[sample_data.VARIANTS[2]] == (2, 0, 2)                    # worse: red, nothing green
        assert expect[sample_data.VARIANTS[3]] == (0, 0, 0)                    # only unsafe: no charts, no good news
        assert expect[sample_data.VARIANTS[4]] == (2, 1, 0)                    # planner says worse, measured faster: no red


def charts_in(block):
    found = []
    for child in block.children.values():
        if getattr(child, "type", "") == "vega_lite_chart":
            found.append(json.loads(child.proto.spec)["layer"][0]["encoding"]["y"]["title"])
        if hasattr(child, "children"):
            found += charts_in(child)
    return found


def markers_in_order(at):
    """Every success / warning / error on the page, in the order they appear (the headline first)."""
    skip = ("No evidence attached", "SAMPLE DATA")
    return [(e.type, e.value) for e in elements(at)
            if getattr(e, "type", "") in ("success", "warning", "error") and not e.value.startswith(skip)]


def test_disagree_rule():
    cost = lambda p: {"pct": p}
    assert tradeoff.disagree(cost(-10.0), {"pct": 30.0}) and tradeoff.disagree(cost(20.0), {"pct": -5.0})
    assert not tradeoff.disagree(cost(20.0), {"pct": 5.0}) and not tradeoff.disagree(cost(-1.0), {"pct": -9.0})
    assert not tradeoff.disagree(cost(0.0), {"pct": 30.0}) and not tradeoff.disagree(cost(10.0), {"pct": 0.0})
    for none in (None, {}, cost(None)):
        assert not tradeoff.disagree(none, {"pct": 30.0}) and not tradeoff.disagree(cost(10.0), none)


def test_page_measured_time_is_the_headline_and_the_planner_estimate_is_collapsed():
    at = run_page(index_result())
    expanders = {e.label: e for e in at.expander}
    box = expanders["Planner estimate (can disagree with measured time)"]
    assert box.proto.expanded is False                                   # collapsed by default
    assert charts_in(box) == ["planner cost units (estimate)"]           # the estimate lives INSIDE it
    assert [c["y_title"] for c in charts_of(at)][0] == "measured milliseconds"   # the headline chart is first
    assert "measured milliseconds" not in charts_in(box)
    assert metrics_of(at)["Measured time improvement"] == "71.8%" and "Estimated cost improvement" in metrics_of(at)


def test_page_without_a_measured_time_keeps_todays_layout():
    at = run_page(index_result(time=None))
    assert "Planner estimate (can disagree with measured time)" not in [e.label for e in at.expander]
    assert [c["y_title"] for c in charts_of(at)] == ["planner cost units (estimate)"]
    assert any(i.value == "Not measured, cost estimate only." for i in at.info)
    assert "Meets the target: more than 50% improvement." in [s.value for s in at.success]   # the cost marker, as before


def test_page_planner_says_worse_but_measured_faster_is_an_amber_note_not_red():
    at = run_page(index_result(cost=(1000.0, 1300.0), time=(100.0, 60.0)))      # cost -30%, measured +40%
    marks = markers_in_order(at)
    print(f"      markers in order: {marks}")
    assert not any(kind == "error" for kind, _ in marks), marks
    assert marks[0] == ("warning", "Below the 50% target.")               # headline: the measured 40%
    assert marks[1][0] == "warning" and "points the other way" in marks[1][1]
    assert "WORSE" not in page_text(at)
    colours = {c["y_title"]: c["after_color"] for c in charts_of(at)}
    assert colours["planner cost units (estimate)"] == "#8a8f98"          # neutral gray, not red
    assert colours["measured milliseconds"] == "#e0a030"
    # the other direction: planner says better, the stopwatch says worse -> headline red, estimate neutral
    at = run_page(index_result(cost=(100.0, 50.0), time=(100.0, 150.0)))
    marks = markers_in_order(at)
    assert marks[0] == ("error", "WORSE: this fix made the query SLOWER when measured.")
    assert not any(kind == "success" for kind, _ in marks) and "points the other way" in marks[1][1]


def test_page_when_both_say_worse_both_stay_red():
    at = run_page(index_result(cost=(1000.0, 1300.0), time=(100.0, 140.0)))
    assert [kind for kind, _ in markers_in_order(at)] == ["error", "error"]
    assert "points the other way" not in page_text(at)


def test_page_target_check_uses_the_measured_improvement():
    at = run_page(index_result(cost=(19437.0, 812.0), time=(100.0, 60.0)))        # cost 96%, measured 40%
    marks = markers_in_order(at)
    assert marks[0] == ("warning", "Below the 50% target.")               # the HEADLINE follows the stopwatch
    assert marks[1] == ("success", "Meets the target: more than 50% improvement.")
    at = run_page(index_result(cost=(100.0, 70.0), time=(100.0, 20.0)))           # cost 30%, measured 80%
    marks = markers_in_order(at)
    assert marks[0] == ("success", "Meets the target: more than 50% improvement.")
    assert marks[1] == ("warning", "Below the 50% target.")


def test_page_tradeoff_shows_only_the_measured_gain_when_there_is_one():
    text = page_text(run_page(index_result()))
    assert "71.8% faster (measured)" in text and "lower estimated cost" not in text
    text = page_text(run_page(index_result(time=None)))
    assert "95.8% lower estimated cost" in text and "faster (measured)" not in text


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
