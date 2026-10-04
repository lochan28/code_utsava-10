"""Tests for the bottleneck card's GNN fields (second opinion, top-3 chart) and the rewrite rule label.

Run:  .venv/Scripts/python -m app.test_gnn_fields      (from the project folder)
"""
import copy
import json

import pyarrow as pa

from app import sample_data
from app.test_before_after import elements
from app.test_recommendations import full, page_text, run_page
from app.ui_helpers import evidence_items, gnn_top3_rows, second_opinion_line

TOP3 = [{"node_id": 2, "op": "Hash Join", "relation": None, "score": 0.21},
        {"node_id": 1, "op": "Seq Scan", "relation": "t_a3f", "score": 0.50},
        {"node_id": 3, "op": "Sort", "relation": None, "score": 0.12}]
AGREES = {"node_id": 1, "op": "Seq Scan", "relation": "t_a3f", "confidence": 0.5, "agrees": True}
DISAGREES = {"node_id": 2, "op": "Hash Join", "relation": None, "confidence": 0.4, "agrees": False}


def bottleneck(second_opinion=None, top3=None, **extra):
    b = copy.deepcopy(sample_data.SAMPLE_BOTTLENECK)
    if second_opinion is not None:
        b["second_opinion"] = second_opinion
    if top3 is not None:
        b["evidence"]["gnn_top3"] = top3
    return {**b, **extra}


def gnn_chart(at):
    for el in elements(at):
        if getattr(el, "type", "") == "vega_lite_chart":
            rows = pa.ipc.open_stream(el.proto.data.data).read_all().to_pylist()
            if rows and "step" in rows[0]:
                return rows
    return None


def test_second_opinion_line_cases():
    assert second_opinion_line(bottleneck(AGREES)) == ("GNN second opinion: agrees (50%)", "green")
    text, color = second_opinion_line(bottleneck(DISAGREES))
    assert text == "GNN second opinion: disagrees and rates Hash Join highest (40%)" and color == "orange"
    assert second_opinion_line(bottleneck(None)) is None and second_opinion_line(None) is None
    for junk in ({"agrees": "yes", "confidence": 0.5}, {"agrees": True, "confidence": 5}, {"agrees": True},
                 {"agrees": True, "confidence": "0.5"}, "text", []):
        assert second_opinion_line(bottleneck(junk)) is None, junk


def test_top3_rows_are_sorted_capped_and_junk_is_dropped():
    rows = gnn_top3_rows(bottleneck(top3=TOP3))
    assert [r["label"] for r in rows] == ["Seq Scan on t_a3f", "Hash Join", "Sort"] and rows[0]["score"] == 0.5
    many = TOP3 + [{"node_id": 9, "op": "Limit", "relation": None, "score": 0.9}]
    assert len(gnn_top3_rows(bottleneck(top3=many))) == 3 and gnn_top3_rows(bottleneck(top3=many))[0]["label"] == "Limit"
    junk = [{"op": "X", "score": 7}, {"op": 3, "score": 0.1}, {"score": 0.1}, "s", None, {"op": "Y", "score": "0.3"}]
    assert gnn_top3_rows(bottleneck(top3=junk)) == [] and gnn_top3_rows(None) == []
    assert gnn_top3_rows({"evidence": {"gnn_top3": "no"}}) == []


def test_page_shows_the_opinion_and_the_chart_with_the_picked_step_highlighted():
    at = run_page(full(bottleneck=bottleneck(AGREES, TOP3)))
    assert ":green-badge[:material/psychology: GNN second opinion: agrees (50%)]" in [m.value for m in at.markdown]
    rows = gnn_chart(at)
    assert [(r["step"], r["score"]) for r in rows] == [("Seq Scan on t_a3f", 50.0), ("Hash Join", 21.0), ("Sort", 12.0)]
    assert [r["step"] for r in rows if r["chosen"]] == ["Seq Scan on t_a3f"]      # node_id 1 is the bottleneck
    assert "The GNN's highest-rated steps" in page_text(at)


def test_page_hides_both_when_ai_sends_none_or_junk_and_still_works():
    for b in (bottleneck(), bottleneck(None, []), bottleneck({"agrees": "x"}, "oops")):
        at = run_page(full(bottleneck=b))
        text = page_text(at)
        assert "GNN second opinion" not in text and gnn_chart(at) is None
        assert "Bottleneck" in text


def test_a_disagreeing_second_opinion_is_orange_not_red():
    at = run_page(full(bottleneck=bottleneck(DISAGREES, TOP3)))
    assert any("orange-badge" in str(m.value) and "disagrees" in str(m.value) for m in at.markdown)
    assert not any("GNN second opinion" in str(e.value) for e in at.error)


def test_the_rewrite_rule_label_is_in_the_evidence_and_nothing_real_is_on_the_page():
    rec = {"kind": "rewrite", "verified_by": "timed_run", "action": "SELECT 1", "reason": "r",
           "rule_label": "filter join to exists"}
    assert ("rewrite rule", "filter join to exists") in evidence_items(rec)
    assert ("rewrite rule", "x") not in evidence_items({**rec, "rule_label": 5})
    at = run_page(full(bottleneck=bottleneck(AGREES, TOP3), recommendations=[rec]))
    text = page_text(at)
    assert "rewrite rule" in text and "filter join to exists" in text
    assert "rahul" not in text.lower() and "customers" not in text.lower()


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
