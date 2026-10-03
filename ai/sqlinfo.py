"""Read a MASKED SQL query and list how each column is used.

Owner: Person 3.  Sees ONLY masked SQL (t_xxx / c_xxx names, ? or $1 values).
It never needs to know what a hashed name really means - only the ROLE a
column plays (exact-match filter, range filter, join key, ...). Those roles
are what decide which index or rewrite could help.

Self-test:  .venv/Scripts/python -m ai.sqlinfo
"""
from collections import defaultdict

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

# ============================================================
# FIXED SETTINGS - SQL facts, not tuning knobs
# ============================================================
RANGE_OPS = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
UNKNOWN_TABLE = "?"   # used when an unqualified column can't be tied to one table

# Roles a column can have (a column can have several):
#   eq_filter     WHERE col = $1                -> index on col is very selective
#   range_filter  WHERE col >= $1 / BETWEEN     -> index works, put it AFTER eq columns
#   func_filter   WHERE f(col) = $1             -> index on col can't be used: rewrite
#   other_filter  WHERE col <> $1 / LIKE ...    -> index rarely helps
#   or_filter     the filter sits inside an OR  -> needs one index per OR branch
#   join          ON a.col = b.col              -> index helps the join lookup
#   not_in        col NOT IN (subquery)         -> rewrite to NOT EXISTS
#   in_subquery   col IN (subquery)
#   correlated    used by a subquery to link to the outer row -> rewrite to JOIN
#   group / order / select                      -> where else the column appears


def parse(masked_sql):
    return sqlglot.parse_one(masked_sql, read="postgres")


def _resolve_table(col, scope):
    """Real (masked) table name behind a column, searching outer scopes for correlated refs."""
    if not col.table:
        # Unqualified column: only safe to place if this scope reads exactly one table.
        tables = [v for v in scope.sources.values() if isinstance(v, exp.Table)]
        return (tables[0].name if len(tables) == 1 else UNKNOWN_TABLE), False
    s = scope
    while s is not None:                       # qualified: look here, then outer queries
        src = s.sources.get(col.table)
        if isinstance(src, exp.Table):
            return src.name, s is not scope   # found in an outer query = correlated
        s = s.parent
    return UNKNOWN_TABLE, False


def _filter_role(col, stop):
    """Role of a column inside a WHERE/ON condition, by looking at what surrounds it."""
    roles = set()
    up = col.parent
    while up is not None and up is not stop:          # any OR above us inside this clause?
        if isinstance(up, exp.Or):
            roles.add("or_filter")
        up = up.parent
    node, under_function = col, False
    while node is not None and node is not stop:
        parent = node.parent
        if isinstance(parent, (exp.Func,)) and not isinstance(parent, (exp.Count, exp.Sum)):
            under_function = True
        if isinstance(parent, exp.In):
            is_not = isinstance(parent.parent, exp.Not)
            if parent.args.get("query") is not None and node is parent.this:
                roles.add("not_in" if is_not else "in_subquery")
                return roles
            if parent.expressions and node is parent.this:
                # col IN ($1, $2, ...): a short list of exact values -> exact-match filter
                # (an index finds each value directly). NOT IN (...) stays "other".
                roles.add("other_filter" if is_not else "eq_filter")
                return roles
        if isinstance(parent, (exp.EQ, exp.NEQ, exp.Like, exp.ILike) + RANGE_OPS):
            other = [c for c in parent.args.values() if isinstance(c, exp.Expression) and c is not node]
            other_is_column = any(isinstance(o, exp.Column) for o in other)
            if under_function:
                roles.add("func_filter")
            elif isinstance(parent, exp.EQ) and other_is_column:
                roles.add("join")
            elif isinstance(parent, exp.EQ):
                roles.add("eq_filter")
            elif isinstance(parent, RANGE_OPS):
                roles.add("range_filter")
            else:
                roles.add("other_filter")
            return roles
        node = parent
    roles.add("other_filter")
    return roles


def _clause_of(col):
    """Which part of the SELECT the column sits in: where / join / group / order / select."""
    node = col
    while node is not None:
        parent = node.parent
        if isinstance(parent, exp.Where):
            return "where", parent
        if isinstance(parent, exp.Join) and node is parent.args.get("on"):
            return "join", parent
        if isinstance(parent, exp.Group):
            return "group", parent
        if isinstance(parent, exp.Order):
            return "order", parent
        if isinstance(parent, exp.Select):
            return "select", parent
        node = parent
    return "select", None


def analyze(masked_sql, column_types=None):
    """Describe a masked query.

    column_types: OPTIONAL {"t_xxx.c_xxx": "date"|"number"|"text"}. Not available
    yet from privacy/; when given, it is copied into each column's info.

    Returns {
      "tables":  ["t_1c1", ...],
      "columns": {"t_1c1.c_02b": {"roles": [...], "type": None|...}, ...},
      "flags":   {"correlated_subquery", "not_in_subquery", "function_on_filter",
                  "or_condition", "placeholders": n, "placeholder_style": "?"|"$n"|"none"},
    }
    """
    tree = parse(masked_sql)
    column_types = column_types or {}
    roles = defaultdict(set)
    tables = []
    flags = {"correlated_subquery": False, "not_in_subquery": False,
             "function_on_filter": False, "or_condition": False}

    for scope in traverse_scope(tree):
        output_names = {e.alias for e in scope.expression.expressions if e.alias} \
            if isinstance(scope.expression, exp.Select) else set()
        for t in scope.expression.find_all(exp.Table):
            if t.name not in tables:
                tables.append(t.name)
        for col in scope.columns:
            if not col.table and col.name in output_names:
                continue                      # ORDER BY x_371 = an output name, not a column
            table, is_outer = _resolve_table(col, scope)
            key = f"{table}.{col.name}"
            clause, stop = _clause_of(col)
            if clause in ("where", "join"):
                r = _filter_role(col, stop)
            else:
                r = {clause}
            if is_outer:
                r.add("correlated")
            roles[key] |= r

    all_roles = set().union(*roles.values()) if roles else set()
    flags["correlated_subquery"] = "correlated" in all_roles
    flags["not_in_subquery"] = "not_in" in all_roles
    flags["function_on_filter"] = "func_filter" in all_roles
    flags["or_condition"] = "or_filter" in all_roles

    bare = list(tree.find_all(exp.Placeholder))       # ?
    numbered = list(tree.find_all(exp.Parameter))     # $1, $2 ...
    flags["placeholders"] = len(bare) + len(numbered)
    flags["placeholder_style"] = ("mixed" if bare and numbered else "$n" if numbered
                                  else "?" if bare else "none")
    return {
        "tables": tables,
        "columns": {k: {"roles": sorted(v), "type": column_types.get(k)}
                    for k, v in sorted(roles.items())},
        "flags": flags,
    }


def join_pairs(masked_sql):
    """[(table_a, col_a, table_b, col_b)] for every  a.x = b.y  linking two different tables,
    including links inside subqueries (correlated / EXISTS)."""
    tree = parse(masked_sql)
    alias = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    pairs = []
    for eq in tree.find_all(exp.EQ):
        a, b = eq.this, eq.expression
        if isinstance(a, exp.Column) and isinstance(b, exp.Column) and a.table in alias and b.table in alias:
            if alias[a.table] != alias[b.table]:
                pairs.append((alias[a.table], a.name, alias[b.table], b.name))
    return pairs


def filter_columns(info):
    """Columns that are worth indexing: used to filter or join (not just displayed)."""
    useful = {"eq_filter", "range_filter", "join", "or_filter", "correlated", "not_in", "in_subquery"}
    return [k for k, v in info["columns"].items() if useful & set(v["roles"])]


if __name__ == "__main__":
    from ai.fixtures import MASKED_QUERIES

    for qid, sql in MASKED_QUERIES.items():
        info = analyze(sql)
        f = info["flags"]
        tags = [k for k in ("correlated_subquery", "not_in_subquery", "function_on_filter", "or_condition") if f[k]]
        print(f"\n{qid}  tables={info['tables']}  placeholders={f['placeholders']} ({f['placeholder_style']})"
              + (f"  FLAGS: {', '.join(tags)}" if tags else ""))
        for col, v in info["columns"].items():
            if set(v["roles"]) - {"select"}:
                print(f"   {col:12} {', '.join(v['roles'])}")

    # Tolerance checks: masked date unit, and optional type info.
    a = analyze("SELECT COUNT(*) FROM t_1c1 WHERE DATE_TRUNC(?, c_02b) = ?")
    b = analyze("SELECT * FROM t_1c1 WHERE c_02b >= $1", {"t_1c1.c_02b": "date"})
    print("\nmasked date unit ->", a["columns"]["t_1c1.c_02b"]["roles"],
          "| with type info ->", b["columns"]["t_1c1.c_02b"])
