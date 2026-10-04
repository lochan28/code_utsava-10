"""Rule-based SQL rewrites on MASKED SQL.

Owner: Person 3.  Sees ONLY masked SQL; works purely on the query's shape.
Each rule fixes one well-known slow pattern and returns SQL that gives the
SAME result (caveats noted per rule). Whether the rewrite is actually faster
is decided later by cost_fn, not here.

Self-test:  .venv/Scripts/python -m ai.rewrite
"""
import sqlglot
from sqlglot import exp

# ============================================================
# FIXED SETTINGS - SQL facts, not tuning knobs
# ============================================================
# Plain-English reason per rule, used by the explanation layer.
RULE_REASONS = {
    "correlated_subquery_to_join":
        "The subquery re-runs once for every outer row. Computing it once, "
        "grouped by the link column, and joining is one pass instead of thousands.",
    "not_in_to_not_exists":
        "NOT IN first builds the full list of subquery values. NOT EXISTS stops at "
        "the first match per row and can use an index. Same result as long as the "
        "subquery column has no NULLs.",
    "function_to_range":
        "Wrapping the column in a function hides it from any index. An equivalent "
        "plain range on the column lets an index be used.",
}
AGGREGATES = (exp.Sum, exp.Min, exp.Max, exp.Avg, exp.Count)
TRUNC_UNITS = {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}


def _parse(sql):
    return sqlglot.parse_one(sql, read="postgres")


def _sql(tree):
    return tree.sql(dialect="postgres")


def _ref(table):
    """Name to qualify a table's columns with: its alias, else its name."""
    return table.alias_or_name


def _from(select):
    """The first table in FROM (sqlglot names this arg "from_" in newer versions, "from" in older)."""
    f = select.args.get("from_") or select.args.get("from")
    return f.this if f is not None else None


def _split_and(cond):
    return list(cond.flatten()) if isinstance(cond, exp.And) else [cond]


# ------------------------------------------------------------
# Rule 1: correlated scalar subquery -> LEFT JOIN on a grouped subquery
#   SELECT a.x, (SELECT SUM(b.v) FROM t2 b WHERE b.k = a.k) AS s FROM t1 a
#   -> SELECT a.x, s1.v AS s FROM t1 a
#      LEFT JOIN (SELECT b.k AS k, SUM(b.v) AS v FROM t2 b GROUP BY b.k) s1 ON s1.k = a.k
# ------------------------------------------------------------
def correlated_subquery_to_join(sql):
    tree = _parse(sql)
    changed, n = False, 0
    for select in list(tree.find_all(exp.Select)):
        if _from(select) is None:
            continue
        outer_refs = {_ref(t) for t in select.find_all(exp.Table) if t.parent_select is select}
        for i, proj in enumerate(list(select.expressions)):
            sub = proj.this if isinstance(proj, exp.Alias) else proj
            if not isinstance(sub, exp.Subquery):
                continue
            inner = sub.this
            if (len(inner.expressions) != 1 or not isinstance(inner.expressions[0], AGGREGATES)
                    or inner.args.get("group") or inner.args.get("joins")
                    or inner.args.get("where") is None):
                continue
            inner_table = _from(inner)
            if not isinstance(inner_table, exp.Table):
                continue
            inner_ref = _ref(inner_table)
            link, rest, ok = None, [], True
            for c in _split_and(inner.args["where"].this):
                cols = list(c.find_all(exp.Column))
                outer_cols = [x for x in cols if x.table in outer_refs and x.table != inner_ref]
                if isinstance(c, exp.EQ) and link is None and len(cols) == 2 and len(outer_cols) == 1:
                    link = (next(x for x in cols if x is not outer_cols[0]), outer_cols[0])
                elif outer_cols:
                    ok = False                     # a second correlation: leave the query alone
                    break
                else:
                    rest.append(c)
            if not ok or link is None:
                continue
            inner_col, outer_col = link
            n += 1
            alias = f"s{n}"
            agg = inner.expressions[0]
            derived = exp.select(
                exp.alias_(inner_col.copy(), "k"), exp.alias_(agg.copy(), "v")
            ).from_(inner_table.copy())
            if rest:
                derived = derived.where(exp.and_(*[r.copy() for r in rest]))
            derived = derived.group_by(inner_col.copy())
            value = exp.column("v", table=alias)
            if isinstance(agg, exp.Count):         # COUNT of no rows is 0; a LEFT JOIN gives NULL
                value = exp.func("COALESCE", value, exp.Literal.number(0))
            new_proj = exp.alias_(value, proj.alias) if isinstance(proj, exp.Alias) else value
            proj.replace(new_proj)
            select.join(exp.alias_(exp.Subquery(this=derived), alias, table=True),
                        on=exp.EQ(this=exp.column("k", table=alias), expression=outer_col.copy()),
                        join_type="left", copy=False)
            changed = True
    return _sql(tree) if changed else None


# ------------------------------------------------------------
# Rule 2: NOT IN (subquery) -> NOT EXISTS
#   WHERE x NOT IN (SELECT y FROM t2)
#   -> WHERE NOT EXISTS (SELECT 1 FROM t2 AS n1 WHERE n1.y = <outer>.x)
# ------------------------------------------------------------
def not_in_to_not_exists(sql):
    tree = _parse(sql)
    changed, n = False, 0
    for not_node in list(tree.find_all(exp.Not)):
        in_node = not_node.this
        if not isinstance(in_node, exp.In) or not isinstance(in_node.args.get("query"), exp.Subquery):
            continue
        outer_col = in_node.this
        inner = in_node.args["query"].this
        if (not isinstance(outer_col, exp.Column) or len(inner.expressions) != 1
                or not isinstance(inner.expressions[0], exp.Column) or inner.args.get("joins")):
            continue
        outer_select = not_node.parent_select
        if not outer_col.table:                    # qualify it; only safe with one outer table
            if outer_select.args.get("joins"):
                continue
            outer_col = exp.column(outer_col.name, table=_ref(_from(outer_select)))
        n += 1
        inner = inner.copy()
        inner_table = _from(inner)
        if not inner_table.alias:
            inner_table.set("alias", exp.TableAlias(this=exp.to_identifier(f"n{n}")))
        inner_col = exp.column(inner.expressions[0].name, table=_ref(inner_table))
        link = exp.EQ(this=inner_col, expression=outer_col.copy())
        inner.set("expressions", [exp.Literal.number(1)])
        old_where = inner.args.get("where")
        inner.set("where", exp.Where(this=exp.and_(old_where.this, link) if old_where else link))
        not_node.replace(exp.Not(this=exp.Exists(this=inner)))
        changed = True
    return _sql(tree) if changed else None


# ------------------------------------------------------------
# Rule 3: function on a column -> plain range
#   WHERE DATE_TRUNC('MONTH', d) = $1
#   -> WHERE (d >= $1 AND d < $1 + INTERVAL '1 MONTH')
# Needs a NUMBERED placeholder ($1): the value is used twice, and a bare ?
# can't be repeated without shifting every later value. Needs the unit kept
# (e.g. 'MONTH'); if privacy/ masks it, the rule can't know the range width.
# ------------------------------------------------------------
def function_to_range(sql):
    tree = _parse(sql)
    changed = False
    for eq in list(tree.find_all(exp.EQ)):
        func, value = eq.this, eq.expression
        if isinstance(value, exp.TimestampTrunc):
            func, value = value, func
        if not isinstance(func, exp.TimestampTrunc) or not isinstance(value, exp.Parameter):
            continue
        unit = func.args.get("unit")
        if not isinstance(unit, exp.Var) or unit.name.upper() not in TRUNC_UNITS:
            continue
        col = func.this
        if not isinstance(col, exp.Column):
            continue
        # CAST: an un-masked plain string like '2026-08-01' + INTERVAL would otherwise
        # be read by Postgres as an interval and fail. TIMESTAMP suits date and timestamp columns.
        upper = exp.Add(this=exp.cast(value.copy(), "TIMESTAMP"),
                        expression=exp.Interval(this=exp.Literal.string("1"), unit=exp.var(unit.name.upper())))
        rng = exp.and_(exp.GTE(this=col.copy(), expression=value.copy()),
                       exp.LT(this=col.copy(), expression=upper))
        eq.replace(exp.Paren(this=rng))
        changed = True
    return _sql(tree) if changed else None


RULES = [correlated_subquery_to_join, not_in_to_not_exists, function_to_range]


def rewrite_sql(masked_sql):
    """All rewrites that apply to this masked query: [{"sql": ..., "rule": ...}].
    Each rule is tried on the original query on its own; rules that don't apply are skipped."""
    out = []
    for rule in RULES:
        try:
            new = rule(masked_sql)
        except Exception:      # a rule must never crash the pipeline; just don't suggest it
            new = None
        if new:
            out.append({"sql": new, "rule": rule.__name__})
    return out


if __name__ == "__main__":
    from ai.fixtures import MASKED_QUERIES

    for qid, sql in MASKED_QUERIES.items():
        for r in rewrite_sql(sql):
            sqlglot.parse_one(r["sql"], read="postgres")   # must still be valid SQL
            print(f"\n{qid}  [{r['rule']}]\n  before: {sql}\n  after:  {r['sql']}")
    hits = {qid for qid, sql in MASKED_QUERIES.items() if rewrite_sql(sql)}
    print("\nqueries rewritten:", sorted(hits))

    print("\nq05 with numbered placeholder ($1):")
    print("  ", rewrite_sql(MASKED_QUERIES["q05"].replace("= ?", "= $1")))
    print("q05 with masked unit:", rewrite_sql("SELECT COUNT(*) FROM t_1c1 WHERE DATE_TRUNC(?, c_02b) = $1"))
