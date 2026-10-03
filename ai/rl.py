"""RL agent that picks the best index / partition / rewrite.

Owner: Person 3.  STATUS: STUB (lead's placeholder until the ai branch is merged).
It just suggests one index on the WHERE columns of the most-filtered table.
Sees ONLY masked data; cost_fn hides the real DB.
Self-test:  .venv/Scripts/python -m ai.rl
"""
import sqlglot
from sqlglot import exp


def _filter_index(masked_sql):
    """'CREATE INDEX ON t_x (c_a, c_b)' for the table with the most WHERE columns."""
    tree = sqlglot.parse_one(masked_sql, read="postgres")
    alias_to_table = {(t.alias or t.name): t.name for t in tree.find_all(exp.Table)}
    where = tree.find(exp.Where)
    by_table = {}
    for col in (where.find_all(exp.Column) if where else []):
        table = alias_to_table.get(col.table) or next(iter(alias_to_table.values()))
        cols = by_table.setdefault(table, [])
        if col.name not in cols:
            cols.append(col.name)
    if not by_table:
        return None
    table, cols = max(by_table.items(), key=lambda kv: len(kv[1]))
    return f"CREATE INDEX ON {table} ({', '.join(cols[:2])})"


def recommend(masked_sql, masked_plan, cost_fn):
    action = _filter_index(masked_sql)
    before = cost_fn(masked_sql, [])
    if action is None:
        return [{"kind": "none", "action": "", "cost_before": before, "cost_after": before,
                 "reason": "No WHERE columns to index."}]
    after = cost_fn(masked_sql, [action])
    return [{"kind": "index", "action": action, "cost_before": before, "cost_after": after,
             "reason": f"Filter columns of {action.split(' ON ')[1]} have no index."}]


if __name__ == "__main__":
    fake_cost = lambda sql, idx: 812.0 if idx else 19437.0
    sql = "SELECT a1.c_1 FROM t_438 AS a1 JOIN t_b78 AS a2 ON a2.c_2 = a1.c_2 WHERE a1.c_13b = $1 AND a1.c_303 >= $2"
    print(recommend(sql, {}, fake_cost))
