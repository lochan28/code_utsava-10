"""RL agent (contextual bandit) that picks the best index / partition / rewrite.

Owner: Person 3.  Sees ONLY masked data; cost_fn hides the real DB.

How it works, simply:
  - It describes the QUERY by its shape only (how many joins, filters, ORs,
    subqueries, how big the biggest table is) - the "state".
  - It describes each candidate FIX by what kind of fix it is (which column
    roles it covers, how selective it is, how wide, how big its table is) -
    the "action". Never by hashed names, so it works on queries it never saw.
  - It keeps a lookup table: "for this kind of query, this kind of fix earned
    this average reward". When a combination is new, it falls back to "this
    kind of fix, on any query", then to "fixes of this type on tables this big".
  - Training = try fixes on practice queries (mostly the best-known one,
    sometimes a random one to keep learning), measure reward with cost_fn,
    update the table.
  - Reward = how much the cost dropped (0..1) minus a write penalty per
    index column (indexes slow down every INSERT/UPDATE).

Self-test (train on fake variants, test on held-out query shapes):
    .venv/Scripts/python -m ai.rl
"""
import random
import time
from collections import defaultdict

from ai import candidates as cand_mod
from ai import sqlinfo

# ============================================================
# TUNABLE SETTINGS
# ============================================================
SEED = 7                  # Fixes exploration randomness -> reproducible results.
EPISODES = 3000           # Training tries. More = better estimates, slower (fake: ~sec;
                          # real HypoPG ~10 ms/try, cached, so still well under 2 min).
EPSILON_START = 0.5       # Chance of trying a random fix early in training (exploring).
EPSILON_END = 0.05        # ...decays to this. Higher = keeps exploring, slower to settle.
WRITE_PENALTY_PER_COL = 0.03   # Reward cost of each index column (slower writes).
                               # Higher = prefers narrow indexes / rewrites; 0 = ignore writes.
PARTITION_WRITE_WIDTH = 1      # Partitioning counted as this many index columns of penalty.
BACKOFF_STRENGTH = 2.0    # How many real tries it takes before a specific (query kind, fix kind)
                          # estimate outweighs the more general fallback estimate.

# ============================================================
# FIXED SETTINGS - bucket edges for describing queries and fixes
# ============================================================
def size_bucket(rows):
    return "small" if rows < 10_000 else "medium" if rows < 1_000_000 else "big"


def sel_bucket(sel):
    if sel is None:
        return "n/a"
    return "tiny" if sel < 0.001 else "small" if sel < 0.02 else "medium" if sel < 0.2 else "large"


# ------------------------------------------------------------
# Features: structure only, never names
# ------------------------------------------------------------
def state_key(info, stats):
    """Shape of the query."""
    roles = [set(v["roles"]) for v in info["columns"].values()]
    count = lambda r: sum(r in x for x in roles)
    biggest = max((stats["rows"].get(t, 0) for t in info["tables"]), default=0)
    f = info["flags"]
    return (
        "joins=" + str(min(len(info["tables"]) - 1, 2)),
        "eq=" + str(min(count("eq_filter"), 2)),
        "range=" + str(min(count("range_filter"), 1)),
        "or" if f["or_condition"] else "-",
        "subq" if f["correlated_subquery"] or f["not_in_subquery"] else "-",
        "func" if f["function_on_filter"] else "-",
        "size=" + size_bucket(biggest),
    )


def _col_role(roles):
    for r, name in (("eq_filter", "eq"), ("range_filter", "range"), ("or_filter", "or"),
                    ("join", "join"), ("correlated", "join"), ("not_in", "join"),
                    ("func_filter", "func"), ("group", "group"), ("order", "sort")):
        if r in roles:
            return name
    return "other"


def action_key(c, info, stats):
    """Kind of fix: role pattern of its columns, selectivity, width, table size."""
    if c["kind"] == "rewrite":
        biggest = max((stats["rows"].get(t, 0) for t in info["tables"]), default=0)
        return ("rewrite", c["rule"], "size=" + size_bucket(biggest))
    (table, cols), = c["cols"].items()
    rows = stats["rows"].get(table, 0)
    roles = [set(info["columns"].get(f"{table}.{col}", {"roles": []})["roles"]) for col in cols]
    if c["kind"] == "partition":
        return ("partition", _col_role(roles[0]),
                "sel=" + sel_bucket(stats["range_sel"].get(f"{table}.{cols[0]}")),
                "size=" + size_bucket(rows))
    pattern = "or-pair" if len(c["sqls"]) > 1 else "+".join(_col_role(r) for r in roles)
    sel = 1.0
    known = False
    for col, r in zip(cols, roles):
        if "eq_filter" in r or "or_filter" in r:
            sel, known = sel * stats["eq_sel"].get(f"{table}.{col}", 1.0), True
        elif "range_filter" in r:
            sel, known = sel * stats["range_sel"].get(f"{table}.{col}", 1.0), True
            break
        else:
            break
    return ("index", pattern, "sel=" + sel_bucket(sel if known else None),
            "width=" + str(len(cols)), "size=" + size_bucket(rows))


def write_width(c):
    if c["kind"] == "rewrite":
        return 0
    if c["kind"] == "partition":
        return PARTITION_WRITE_WIDTH
    return sum(len(cols) for cols in c["cols"].values()) if len(c["sqls"]) == 1 else len(c["sqls"])


def evaluate(masked_sql, c, cost_fn, before=None):
    """Measure one fix: (cost_before, cost_after, reward)."""
    before = cost_fn(masked_sql, []) if before is None else before
    after = cost_fn(c["sqls"][0], []) if c["kind"] == "rewrite" else cost_fn(masked_sql, c["sqls"])
    drop = max(0.0, (before - after) / before) if before > 0 else 0.0
    return before, after, drop - WRITE_PENALTY_PER_COL * write_width(c)


# ------------------------------------------------------------
# The agent
# ------------------------------------------------------------
class BanditAgent:
    def __init__(self, seed=SEED):
        self.rng = random.Random(seed)
        # three levels of the lookup table: [sum of rewards, number of tries]
        self.specific = defaultdict(lambda: [0.0, 0])   # (query kind, fix kind)
        self.general = defaultdict(lambda: [0.0, 0])    # fix kind on any query
        self.coarse = defaultdict(lambda: [0.0, 0])     # fix type + table size

    @staticmethod
    def _coarse(a):
        return (a[0], a[-1])

    def score(self, s, a):
        """Estimated reward, blending specific -> general -> coarse knowledge."""
        k = BACKOFF_STRENGTH
        sc, nc = self.coarse[self._coarse(a)]
        m3 = sc / nc if nc else 0.0
        sg, ng = self.general[a]
        m2 = (sg + k * m3) / (ng + k)
        ss, ns = self.specific[(s, a)]
        return (ss + k * m2) / (ns + k)

    def update(self, s, a, reward):
        for table, key in ((self.specific, (s, a)), (self.general, a), (self.coarse, self._coarse(a))):
            table[key][0] += reward
            table[key][1] += 1

    def rank(self, masked_sql, stats, column_types=None):
        """All candidates, best predicted first: [(score, candidate, action_key)]."""
        info = sqlinfo.analyze(masked_sql, column_types)
        s = state_key(info, stats)
        cands = cand_mod.candidates(masked_sql, column_types)
        scored = [(self.score(s, action_key(c, info, stats)), i, c, action_key(c, info, stats))
                  for i, c in enumerate(cands)]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return s, [(sc, c, a) for sc, _, c, a in scored]

    def train(self, episodes, n=EPISODES):
        """episodes: [(masked_sql, stats, cost_fn)]. Results are cached per (query, fix),
        so with a real DB each distinct fix is simulated only once."""
        cache, prepared = {}, []
        for i, (sql, stats, cost_fn) in enumerate(episodes):
            s, ranked = self.rank(sql, stats)
            prepared.append((i, sql, s, ranked, cost_fn))
        for step in range(n):
            eps = EPSILON_START + (EPSILON_END - EPSILON_START) * step / max(1, n - 1)
            i, sql, s, ranked, cost_fn = self.rng.choice(prepared)
            if not ranked:
                continue
            if self.rng.random() < eps:
                _, c, a = self.rng.choice(ranked)
            else:   # best by CURRENT estimates (re-score: estimates change while learning)
                _, c, a = max(ranked, key=lambda x: self.score(s, x[2]))
            key = (i, tuple(c["sqls"]))
            if key not in cache:
                cache[key] = evaluate(sql, c, cost_fn)[2]
            self.update(s, a, cache[key])
        return len(cache)

    def choose(self, masked_sql, stats, column_types=None):
        """Best predicted fix WITHOUT calling cost_fn (the point of learning)."""
        _, ranked = self.rank(masked_sql, stats, column_types)
        return ranked[0][1] if ranked else None


# ------------------------------------------------------------
# Baselines for comparison
# ------------------------------------------------------------
def index_every_filter_column(masked_sql):
    """Naive DBA move: one single-column index on every filter / join column."""
    info = sqlinfo.analyze(masked_sql)
    useful = {"eq_filter", "range_filter", "or_filter", "join", "correlated", "not_in"}
    sqls, cols = [], {}
    for key, v in info["columns"].items():
        t, c = key.split(".", 1)
        if t != sqlinfo.UNKNOWN_TABLE and useful & set(v["roles"]):
            sqls.append(f"CREATE INDEX ON {t} ({c})")
            cols.setdefault(t, []).append(c)
    return {"kind": "index", "sqls": sqls, "cols": cols, "why": "baseline"}


def recommend(masked_sql, masked_plan, cost_fn):
    # STUB until step 7: wired to the trained agent there.
    action = "CREATE INDEX ON t_a3f (c_91b, c_d42)"
    before, after = cost_fn(masked_sql, []), cost_fn(masked_sql, [action])
    return [{"kind": "index", "action": action, "cost_before": before, "cost_after": after,
             "reason": "Filter columns c_91b, c_d42 have no index."}]


if __name__ == "__main__":
    from ai import fake_env

    variants = fake_env.make_variants()
    train, test = fake_env.split(variants)
    episodes = [(v["sql"], fake_env.stats_for_agent(v["catalog"]), fake_env.make_cost_fn(v["catalog"]))
                for v in train]

    t0 = time.time()
    agent = BanditAgent()
    n_sims = agent.train(episodes)
    print(f"trained on {len(train)} variants of shapes {sorted({v['shape'] for v in train})}")
    print(f"  {EPISODES} episodes, {n_sims} distinct simulations, {time.time() - t0:.1f} s")

    def row(v, c, before, f):
        _, after, reward = evaluate(v["sql"], c, f, before)
        drop = 100 * (1 - after / before)
        return drop, WRITE_PENALTY_PER_COL * write_width(c), reward

    print(f"\nHELD-OUT query shapes {sorted({v['shape'] for v in test})} - never seen in training")
    print(f"{'query':7} | {'RL pick (no simulation)':44} {'drop%':>6} {'pen':>5} | "
          f"{'random':>6} {'pen':>5} | {'index-all':>9} {'pen':>5} | {'best%':>6}")
    totals = defaultdict(list)
    for v in test:
        stats, f = fake_env.stats_for_agent(v["catalog"]), fake_env.make_cost_fn(v["catalog"])
        before = f(v["sql"], [])
        cands = cand_mod.candidates(v["sql"])
        rl = row(v, agent.choose(v["sql"], stats), before, f)
        rnd = [row(v, c, before, f) for c in cands]
        rnd = tuple(sum(x[i] for x in rnd) / len(rnd) for i in range(3))   # expected value of a random pick
        allx = row(v, index_every_filter_column(v["sql"]), before, f)
        best = max(row(v, c, before, f) for c in cands)                   # oracle: tries every fix
        pick = agent.choose(v["sql"], stats)
        label = pick["rule"] if pick["kind"] == "rewrite" else " + ".join(pick["sqls"])
        print(f"{v['id']:7} | {label[:44]:44} {rl[0]:6.1f} {rl[1]:5.2f} | {rnd[0]:6.1f} {rnd[1]:5.2f} | "
              f"{allx[0]:9.1f} {allx[1]:5.2f} | {best[0]:6.1f}")
        for name, r in (("RL", rl), ("random", rnd), ("index-all", allx), ("oracle", best)):
            totals[name].append(r)
    print("\nAVERAGE over held-out queries:")
    print(f"  {'method':10} {'cost drop %':>12} {'write pen':>10} {'reward':>8}")
    for name, rs in totals.items():
        avg = [sum(r[i] for r in rs) / len(rs) for i in range(3)]
        print(f"  {name:10} {avg[0]:12.1f} {avg[1]:10.2f} {avg[2]:8.3f}")
