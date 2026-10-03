"""RL agent that picks the best index / partition / rewrite.

Owner: Person 3.  STATUS: STUB.  Sees ONLY masked data; cost_fn hides the real DB.
Self-test:  .venv/Scripts/python -m ai.rl
"""


def recommend(masked_sql, masked_plan, cost_fn):
    action = "CREATE INDEX ON t_a3f (c_91b, c_d42)"
    before, after = cost_fn(masked_sql, []), cost_fn(masked_sql, [action])
    return [{"kind": "index", "action": action, "cost_before": before, "cost_after": after,
             "reason": "Filter columns c_91b, c_d42 have no index."}]


if __name__ == "__main__":
    fake_cost = lambda sql, idx: 812.0 if idx else 19437.0
    print(recommend("SELECT ...", {}, fake_cost))
