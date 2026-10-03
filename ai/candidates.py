"""List every fix worth trying for one MASKED query: the RL agent's possible actions.

Owner: Person 3.  Sees ONLY masked SQL (via ai.sqlinfo).
This file only PROPOSES; it never decides. Which candidate is best is the RL
agent's job (ai/rl.py), using cost_fn to measure.

Self-test:  .venv/Scripts/python -m ai.candidates
"""
from itertools import combinations

from ai import rewrite, sqlinfo

# ============================================================
# TUNABLE SETTINGS
# ============================================================
MAX_INDEX_COLS = 3     # Widest composite index proposed. Wider = can match more filters
                       # at once, but bigger on disk and slower writes. 2-3 is typical.
MAX_EQ_COLS_IN_COMPOSITE = 2   # How many exact-match columns to combine before the range
                               # column. Higher = more candidates to test (slower search).
PARTITION_SCHEMES = ["MONTHLY"]  # Partition sizes to propose, e.g. add "YEARLY".
                                 # Each extra scheme = one more candidate per date-like column.
PROPOSE_PARTITIONS_WITHOUT_TYPES = True  # Column types aren't available from privacy/ yet.
                                         # True: propose partitioning on any range-filtered
                                         # column and let cost_fn judge. False: only when
                                         # the column is known to be a date.
PARTITION_SKIP_KEY_COLUMNS = True  # TEMPORARY guess while column types are missing: don't
                                   # partition on a column that is also a join/lookup key
                                   # (keys like customer_id are rarely good partition columns;
                                   # dates are rarely join keys). Ignored once types exist.

# ============================================================
# FIXED SETTINGS - formats agreed with the team
# ============================================================
# Partition action text (PROPOSED to Person 1 / lead; sandbox would build it on a copy):
PARTITION_FORMAT = "PARTITION {table} BY RANGE ({col}) {scheme}"


def _index_sql(table, cols):
    return f"CREATE INDEX ON {table} ({', '.join(cols)})"


def _by_table(info):
    """{table: {role: [cols]}} for columns that could use an index."""
    out = {}
    for key, v in info["columns"].items():
        table, col = key.split(".", 1)
        if table == sqlinfo.UNKNOWN_TABLE:
            continue                       # can't place an index on an unknown table
        for role in v["roles"]:
            out.setdefault(table, {}).setdefault(role, [])
            if col not in out[table][role]:
                out[table][role].append(col)
    return out


def index_candidates(info):
    """Single and composite indexes from how columns are used.
    Returns a list of {"kind": "index", "sqls": [...], "cols": {table: [cols]}, "why": str}."""
    found, seen = [], set()

    def add(table, cols, why):
        key = (table, tuple(cols))
        if key not in seen and 1 <= len(cols) <= MAX_INDEX_COLS:
            seen.add(key)
            found.append({"kind": "index", "sqls": [_index_sql(table, cols)],
                          "cols": {table: list(cols)}, "why": why})

    for table, roles in _by_table(info).items():
        eq = roles.get("eq_filter", [])
        rng = roles.get("range_filter", [])
        lookups = roles.get("join", []) + roles.get("correlated", []) + roles.get("not_in", []) \
            + roles.get("in_subquery", [])
        or_cols = set(roles.get("or_filter", []))
        # 1) one column at a time
        for c in eq:
            add(table, [c], "exact-match filter")
        for c in rng:
            add(table, [c], "range filter")
        for c in lookups:
            add(table, [c], "join / subquery lookup")
        # 2) composites: exact-match columns first, then at most one range column
        #    (an index can use any number of = columns, but only the first range column)
        plain_eq = [c for c in eq if c not in or_cols]
        for k in range(1, min(MAX_EQ_COLS_IN_COMPOSITE, len(plain_eq)) + 1):
            for combo in combinations(plain_eq, k):
                if k > 1:
                    add(table, list(combo), "several exact-match filters")
                for r in rng:
                    add(table, list(combo) + [r], "exact-match filter(s) then range")
        # 3) join column + range column on the same table (filter and join in one index)
        for j in lookups:
            for r in rng:
                if j != r:
                    add(table, [j, r], "join column then range filter")
    return found


def or_candidates(info):
    """A filter like  a = $1 OR b = $2  needs one index per side, used together."""
    found = []
    for table, roles in _by_table(info).items():
        or_cols = roles.get("or_filter", [])
        if len(or_cols) >= 2:
            found.append({"kind": "index", "sqls": [_index_sql(table, [c]) for c in or_cols],
                          "cols": {table: list(or_cols)}, "why": "one index per OR branch"})
    return found


KEY_ROLES = {"join", "correlated", "not_in", "in_subquery"}


def partition_column_ok(roles, col_type=None):
    """Is this column worth partitioning on?  The ONE place to change when privacy/
    starts sending column types: then simply  return col_type == "date"  (plus the
    range check)."""
    if not {"range_filter", "func_filter"} & roles:
        return False                     # exact-match-only or join-only: never
    if col_type is not None:
        return col_type == "date"        # real type known: trust it
    if not PROPOSE_PARTITIONS_WITHOUT_TYPES:
        return False
    if PARTITION_SKIP_KEY_COLUMNS and roles & KEY_ROLES:
        return False                     # temporary guess: keys aren't partition columns
    return True


def partition_candidates(info):
    found = []
    for key, v in info["columns"].items():
        table, col = key.split(".", 1)
        if table == sqlinfo.UNKNOWN_TABLE:
            continue
        if not partition_column_ok(set(v["roles"]), v.get("type")):
            continue
        known_type = v.get("type")
        for scheme in PARTITION_SCHEMES:
            found.append({"kind": "partition",
                          "sqls": [PARTITION_FORMAT.format(table=table, col=col, scheme=scheme)],
                          "cols": {table: [col]},
                          "why": "range filter on this column; partitions let the scan skip "
                                 "whole months" + ("" if known_type else " (column type unknown)")})
    return found


def rewrite_candidates(masked_sql):
    return [{"kind": "rewrite", "sqls": [r["sql"]], "cols": {}, "rule": r["rule"],
             "why": rewrite.RULE_REASONS[r["rule"]]} for r in rewrite.rewrite_sql(masked_sql)]


def candidates(masked_sql, column_types=None):
    """All fix candidates for one masked query, in a stable order."""
    info = sqlinfo.analyze(masked_sql, column_types)
    return (index_candidates(info) + or_candidates(info)
            + partition_candidates(info) + rewrite_candidates(masked_sql))


if __name__ == "__main__":
    from ai.fixtures import MASKED_QUERIES

    queries = dict(MASKED_QUERIES)
    queries["q05"] = queries["q05"].replace("= ?", "= $1")   # numbered, so the range rewrite can apply

    # The fix we expect for each query (masked names), from db/queries.py "fix_hint".
    EXPECTED = {
        "q01": "CREATE INDEX ON t_1c1 (c_1e3)",
        "q02": "CREATE INDEX ON t_1c1 (c_02b)",
        "q03": "CREATE INDEX ON t_1c1 (c_073, c_02b)",
        "q04": "correlated_subquery_to_join",
        "q05": "function_to_range",
        "q06": "CREATE INDEX ON t_1c1 (c_c3a, c_02b)",
        "q07": "not_in_to_not_exists",
        "q08": "PARTITION t_1c1 BY RANGE (c_02b) MONTHLY",
        "q09": "CREATE INDEX ON t_1c1 (c_1e3) + CREATE INDEX ON t_1c1 (c_c3a)",
        "q10": "CREATE INDEX ON t_1c1 (c_c3a)",
        "q11": "CREATE INDEX ON t_1c1 (c_0b8, c_02b)",
    }
    all_ok = True
    for qid, sql in queries.items():
        cands = candidates(sql)
        labels = [" + ".join(c["sqls"]) if c["kind"] != "rewrite" else c["rule"] for c in cands]
        ok = EXPECTED[qid] in labels
        all_ok &= ok
        kinds = {k: sum(c["kind"] == k for c in cands) for k in ("index", "partition", "rewrite")}
        print(f"\n{qid}: {len(cands)} candidates {kinds}   expected fix present: {'YES' if ok else 'NO'}")
        for lab, c in zip(labels, cands):
            mark = "  <== expected" if lab == EXPECTED[qid] else ""
            print(f"   [{c['kind']:9}] {lab[:75]:75} ({c['why'][:30]}){mark}")
    print("\nALL EXPECTED FIXES PRESENT:", all_ok)
