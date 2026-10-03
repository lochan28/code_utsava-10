"""Practice world for the RL agent: fake query variants + a FAKE cost model.

Owner: Person 3.  Dev tool only - replaced by the real HypoPG cost_fn in step 5.
Everything here is masked: variants are re-hashed copies of ai/fixtures.py
with made-up table sizes and selectivities, so the agent can't memorise names.

The fake cost model is deterministic and depends only on query STRUCTURE and
table statistics, roughly like a real planner:
  - a full scan costs one unit per row;
  - an index costs RANDOM_IO per matching row (+ a small lookup cost), so it
    only wins when it matches few rows;
  - partitions skip whole months, but only for range filters on date columns;
  - a correlated subquery re-runs once per outer row; NOT IN builds a big list;
    a function around a column hides it from indexes and costs CPU per row.

Self-test:  .venv/Scripts/python -m ai.fake_env
"""
import hashlib
import math
import random
import re

import sqlglot
from sqlglot import exp

from ai import sqlinfo
from ai.fixtures import MASKED_QUERIES

# ============================================================
# TUNABLE SETTINGS
# ============================================================
SEED = 7                    # Same seed -> same variants and same results every run.
VARIANTS_PER_SHAPE = 4      # 11 shapes x 4 = 44 queries. More = more practice data.
TEST_SHAPES = {"q03", "q06", "q11"}  # Whole query SHAPES held out for testing (~27%).
                                     # The agent never trains on any variant of these.
BIG_TABLE_SPREAD = (-1.0, 1.3)  # Big table size = base x 10^U(lo, hi): 100k .. 20M rows.
STAT_SPREAD = 0.3               # Other sizes/selectivities jitter by x 10^U(-0.3, 0.3).
RANDOM_IO = 4.0             # How much dearer an index row-fetch is than a sequential
                            # row read. Higher = indexes only win on very few rows.

# ============================================================
# FIXED SETTINGS - base statistics of the fixture schema (masked names)
# ============================================================
BASE_ROWS = {"t_1c1": 1_000_000, "t_21e": 50_000, "t_0a3": 2_000, "t_f23": 10}
BASE_NDISTINCT = {"c_1e3": 50_000, "c_c3a": 2_000, "c_0b8": 10, "c_073": 5, "c_03e": 3,
                  "c_edb": 8, "c_82a": 256, "c_ca1": 1_000_000, "c_02b": 1_095}
BASE_RANGE_SEL = {"q02": {"c_02b": 0.028}, "q03": {"c_02b": 0.01}, "q04": {"c_1e3": 0.006},
                  "q05": {"c_02b": 0.028}, "q06": {"c_02b": 0.08}, "q08": {"c_02b": 0.33},
                  "q11": {"c_02b": 0.028}}
DATE_COLS = {"c_02b"}
PRIMARY_KEYS = {"t_1c1": "c_ca1", "t_21e": "c_1e3", "t_0a3": "c_c3a", "t_f23": "c_0b8"}
MONTHS_OF_DATA = 36
NAME = re.compile(r"\b([tcx])_[0-9a-f]{3}\b")


# ------------------------------------------------------------
# Variants
# ------------------------------------------------------------
def _rename(sql, salt):
    """Give every masked name a new hash, consistently within one variant."""
    mapping = {}

    def new(m):
        old = m.group(0)
        if old not in mapping:
            mapping[old] = f"{m.group(1)}_{hashlib.sha256((salt + old).encode()).hexdigest()[:3]}"
        return mapping[old]

    out = NAME.sub(new, sql)
    assert len(set(mapping.values())) == len(mapping), "hash collision; change SEED"
    return out, mapping


def make_variants(seed=SEED):
    """[{"id", "shape", "sql", "catalog"}] - catalog is the fake DB statistics."""
    rng = random.Random(seed)
    jitter = lambda: 10 ** rng.uniform(-STAT_SPREAD, STAT_SPREAD)
    out = []
    for shape, base_sql in MASKED_QUERIES.items():
        if shape == "q05":
            base_sql = base_sql.replace("= ?", "= $1")      # numbered, so the range rewrite applies
        for v in range(VARIANTS_PER_SHAPE):
            sql, m = (base_sql, {}) if v == 0 else _rename(base_sql, f"{seed}-{shape}-{v}")
            r = lambda name: m.get(name, name)
            if v == 0:
                big, js = 1.0, (lambda: 1.0)
            else:
                big, js = 10 ** rng.uniform(*BIG_TABLE_SPREAD), jitter
            rows = {r(t): max(5, int(n * (big if t == "t_1c1" else js()))) for t, n in BASE_ROWS.items()}
            nd = {r(c): max(2, int(n * js())) for c, n in BASE_NDISTINCT.items()}
            rsel = {r(c): min(1.0, s * js()) for c, s in BASE_RANGE_SEL.get(shape, {}).items()}
            out.append({
                "id": f"{shape}v{v}", "shape": shape, "sql": sql,
                "catalog": {"rows": rows, "ndistinct": nd, "range_sel": rsel,
                            "date_cols": {r(c) for c in DATE_COLS},
                            "pk": {r(t): r(c) for t, c in PRIMARY_KEYS.items()}},
            })
    return out


def split(variants):
    train = [v for v in variants if v["shape"] not in TEST_SHAPES]
    test = [v for v in variants if v["shape"] in TEST_SHAPES]
    return train, test


def stats_for_agent(catalog):
    """What the agent may see: table sizes and filter selectivities, keyed by masked
    names. (In step 5 these come from the masked EXPLAIN plan instead.)"""
    eq_sel = {}
    for t, rows in catalog["rows"].items():
        for c, n in catalog["ndistinct"].items():
            eq_sel[f"{t}.{c}"] = 1.0 / min(n, rows)
    rsel = {f"{t}.{c}": s for t in catalog["rows"] for c, s in catalog["range_sel"].items()}
    return {"rows": dict(catalog["rows"]), "eq_sel": eq_sel, "range_sel": rsel}


# ------------------------------------------------------------
# Fake cost model
# ------------------------------------------------------------
_INDEX = re.compile(r"CREATE INDEX ON (\w+) \(([^)]*)\)")
_PART = re.compile(r"PARTITION (\w+) BY RANGE \((\w+)\)")


def _parse_actions(actions):
    idx, parts = {}, {}
    for a in actions:
        if m := _INDEX.match(a):
            idx.setdefault(m.group(1), []).append([c.strip() for c in m.group(2).split(",")])
        elif m := _PART.match(a):
            parts[m.group(1)] = m.group(2)
    return idx, parts


def _correlated_inner_tables(tree):
    """Tables inside a scalar subquery in the SELECT list (re-run once per outer row)."""
    inner = set()
    for sub in tree.find_all(exp.Subquery):
        holder = sub.parent.parent if isinstance(sub.parent, exp.Alias) else sub.parent
        if isinstance(holder, exp.Select) and sub.parent in holder.expressions or sub in getattr(holder, "expressions", []):
            inner |= {t.name for t in sub.find_all(exp.Table)}
    return inner


def _join_pairs(tree):
    """[(table_a, col_a, table_b, col_b)] for every  a.x = b.y  between two tables."""
    alias = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    pairs = []
    for eq in tree.find_all(exp.EQ):
        a, b = eq.this, eq.expression
        if isinstance(a, exp.Column) and isinstance(b, exp.Column) and a.table in alias and b.table in alias:
            ta, tb = alias[a.table], alias[b.table]
            if ta != tb:
                pairs.append((ta, a.name, tb, b.name))
    return pairs


def fake_cost(sql, actions, catalog):
    tree = sqlglot.parse_one(sql, read="postgres")
    info = sqlinfo.analyze(sql)
    idx, parts = _parse_actions(actions)
    for t, c in catalog["pk"].items():                      # primary keys always exist
        idx.setdefault(t, []).append([c])
    rows_of, nd, rsel = catalog["rows"], catalog["ndistinct"], catalog["range_sel"]
    eq_sel = lambda t, c: 1.0 / min(nd.get(c, 100), rows_of[t])

    roles = {}
    for key, v in info["columns"].items():
        t, c = key.split(".", 1)
        if t in rows_of:
            for r in v["roles"]:
                roles.setdefault(t, {}).setdefault(r, set()).add(c)

    def scan(t):
        rows, R = rows_of[t], roles.get(t, {})
        orc = R.get("or_filter", set())
        eq = R.get("eq_filter", set()) - orc
        rng = R.get("range_filter", set())
        func = R.get("func_filter", set())
        frac = 1.0
        for c in eq:
            frac *= eq_sel(t, c)
        for c in rng | func:
            frac *= rsel.get(c, 0.3)
        if orc:
            frac *= min(1.0, sum(eq_sel(t, c) for c in orc))
        cost = rows * 1.0 + rows * 0.5 * bool(func)          # function: CPU per row
        for cols in idx.get(t, []):
            s, used = 1.0, 0
            for c in cols:
                if c in eq:
                    s, used = s * eq_sel(t, c), used + 1
                elif c in rng:
                    s, used = s * rsel.get(c, 0.3), used + 1
                    break
                else:
                    break
            if used:
                cost = min(cost, rows * s * RANDOM_IO + math.log2(rows + 1) * 10)
        if orc and all(any(cols[0] == c for cols in idx.get(t, [])) for c in orc):
            cost = min(cost, rows * sum(eq_sel(t, c) for c in orc) * RANDOM_IO + 20)
        if t in parts:
            col = parts[t]
            if col in rng and col in catalog["date_cols"]:
                cost = min(cost, rows * max(rsel.get(col, 1.0), 1 / MONTHS_OF_DATA) * 1.05 + 50)
            else:
                cost *= 1.05                                  # routing overhead, no pruning
        return cost, rows * frac

    pairs = _join_pairs(tree)
    inner = _correlated_inner_tables(tree)
    outer_tables = [t for t in info["tables"] if t in rows_of and t not in inner]
    scans = {t: scan(t) for t in outer_tables}
    total = 0.0
    for t, (cost, out_rows) in scans.items():
        # nested-loop alternative: for each table JOINED to t, look t's rows up through
        # an index on t's join column, once per (filtered) row of that partner table
        for (ta, ca, tb, cb) in pairs:
            for (me, my_col, partner) in ((ta, ca, tb), (tb, cb, ta)):
                if me == t and partner in scans and any(cols[0] == my_col for cols in idx.get(t, [])):
                    drive = scans[partner][1]
                    cost = min(cost, drive * (math.log2(rows_of[t] + 1) + rows_of[t] * eq_sel(t, my_col) * RANDOM_IO))
        total += cost
    if len(outer_tables) > 1:
        total += sum(o for _, o in scans.values()) * 0.2       # hash join build/probe
    if any(isinstance(n.this, exp.In) and n.this.args.get("query") for n in tree.find_all(exp.Not)):
        sub_tables = {t.name for n in tree.find_all(exp.Not) if isinstance(n.this, exp.In)
                      for t in n.find_all(exp.Table)}
        total += sum(rows_of[t] * 0.8 for t in sub_tables if t in rows_of)
    for t in inner:                                            # correlated: once per outer row
        if t not in rows_of:
            continue
        outer_rows = min(o for _, o in scans.values()) if scans else 1
        link = roles.get(t, {}).get("join", set())
        per_row = rows_of[t]
        for j in link:
            if any(cols[0] == j for cols in idx.get(t, [])):
                per_row = min(per_row, math.log2(rows_of[t] + 1) + rows_of[t] * eq_sel(t, j) * RANDOM_IO)
        total += outer_rows * per_row
    return total


def make_cost_fn(catalog):
    """cost_fn(masked_sql, masked_action_sqls) -> float, same signature as the real one."""
    return lambda sql, actions: fake_cost(sql, actions, catalog)


if __name__ == "__main__":
    from ai.candidates import candidates

    variants = make_variants()
    train, test = split(variants)
    print(f"{len(variants)} variants: {len(train)} train ({sorted({v['shape'] for v in train})}), "
          f"{len(test)} test ({sorted({v['shape'] for v in test})})")
    q11_names = [sorted(m.group(0) for m in NAME.finditer(v["sql"]))[:3] for v in variants if v["shape"] == "q11"]
    print("q11 variant names differ:", len({tuple(n) for n in q11_names}) == len(q11_names), q11_names)
    print("\nbase variants (v0): cost before -> best single fix")
    for v in variants:
        if not v["id"].endswith("v0"):
            continue
        f = make_cost_fn(v["catalog"])
        before = f(v["sql"], [])
        res = []
        for c in candidates(v["sql"]):
            sql, acts = (c["sqls"][0], []) if c["kind"] == "rewrite" else (v["sql"], c["sqls"])
            res.append((f(sql, acts), " + ".join(c["sqls"]) if c["kind"] != "rewrite" else c["rule"]))
        after, best = min(res)
        print(f"  {v['shape']}: {before:>14,.0f} -> {after:>12,.0f} ({100 * (1 - after / before):5.1f}%)  {best[:60]}")
