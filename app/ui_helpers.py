"""Small pure helpers for the dashboard (no Streamlit inside, so they are easy to test).

Self-test:  .venv/Scripts/python -m app.ui_helpers
"""


def n(x):
    """12345.6 -> '12,346'; anything that is not a number -> '?'."""
    return f"{x:,.0f}" if isinstance(x, (int, float)) else "?"


def plan_tree_text(masked_plan):
    """The masked plan as an indented tree, one step per line."""
    if isinstance(masked_plan, list):          # EXPLAIN (FORMAT JSON) wraps the plan in a list
        masked_plan = masked_plan[0] if masked_plan else {}
    root = (masked_plan or {}).get("Plan")
    lines = []

    def walk(node, depth):
        where = f" on {node['Relation Name']}" if node.get("Relation Name") else ""
        lines.append(f"{'   ' * depth}{'-> ' if depth else ''}{node.get('Node Type', '?')}{where}"
                     f"  (cost {n(node.get('Total Cost'))}, ~{n(node.get('Plan Rows'))} rows)")
        for child in node.get("Plans", []):
            walk(child, depth + 1)

    if root:
        walk(root, 0)
    return "\n".join(lines) or "(no plan available)"


def match_line(m):
    """'Matched X (q11), 82% sure - method - runner-up' from pipeline's match dict."""
    if not m:
        return ""
    line = f"Matched **{m['name']}** ({m['query_id']}), {m['confidence']:.0%} sure · {m['method']}"
    return line + (f" · runner-up: {m['runner_up']}" if m.get("runner_up") else "")


if __name__ == "__main__":
    plan = {"Plan": {"Node Type": "Hash Join", "Total Cost": 20000.0, "Plan Rows": 900, "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "t_a3f", "Total Cost": 19437.0, "Plan Rows": 1200},
        {"Node Type": "Hash", "Total Cost": 50.0, "Plan Rows": 10, "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "t_b22", "Total Cost": 20.0, "Plan Rows": 10}]}]}}
    print(plan_tree_text(plan))
    print(match_line({"name": "Weekly sales report", "query_id": "q11", "confidence": 0.82,
                      "method": "keywords", "runner_up": "q02"}))
