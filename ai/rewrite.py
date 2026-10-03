"""Rule-based SQL rewrites (subquery -> JOIN, NOT IN -> NOT EXISTS, ...).

Owner: Person 3.  STATUS: STUB.  Sees ONLY masked SQL.
Self-test:  .venv/Scripts/python -m ai.rewrite
"""


def rewrite_sql(masked_sql):
    return []   # list of {"sql": ..., "rule": ...}


if __name__ == "__main__":
    print(rewrite_sql("SELECT ... FROM t_a3f WHERE c_91b NOT IN (SELECT ...)"))
