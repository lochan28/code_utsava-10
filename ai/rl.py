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
import ast
import json
import random
import time
from collections import defaultdict
from pathlib import Path

from ai import candidates as cand_mod
from ai import sqlinfo, verify, why_not

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
VERIFY_TOP_K = 3          # Safety net: really simulate the agent's top K picks with cost_fn
                          # and keep the best. Costs K+1 cost_fn calls per query (1 = baseline).
                          # 0 = trust the agent blindly; higher = safer but more simulations.
FALLBACK = "selectivity-first"   # What the agent keeps when a fix kind is new to it:
    # "pattern-first":     exact fix -> role pattern + table size -> role pattern -> fix type
    #                      (keeps WHICH columns; forgets how selective / how big)
    # "selectivity-first": exact fix -> selectivity + table size -> table size -> fix type
    #                      (keeps HOW MUCH it filters and how big the table is)

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
def drive_bucket(ratio):
    """Rows driving an index lookup, relative to the indexed table's size."""
    if ratio is None:
        return "drive=n/a"
    return "drive=" + ("tiny" if ratio < 0.001 else "small" if ratio < 0.05 else "large")


def filtered_rows(table, info, stats):
    """Estimated rows of a table after its own exact-match and range filters."""
    rows = stats["rows"].get(table, 0)
    for key, v in info["columns"].items():
        t, c = key.split(".", 1)
        if t == table:
            if "eq_filter" in v["roles"] and "or_filter" not in v["roles"]:
                rows *= stats["eq_sel"].get(key, 1.0)
            if "range_filter" in v["roles"]:
                rows *= stats["range_sel"].get(key, 1.0)
    return rows


def driving_ratio(table, col, info, stats, pairs):
    """For an index starting with join column `col` on `table`: how many rows would look
    it up (the smallest filtered partner table), relative to the table's size."""
    partners = [tb for ta, ca, tb, cb in pairs if (ta, ca) == (table, col)] +                [ta for ta, ca, tb, cb in pairs if (tb, cb) == (table, col)]
    rows = stats["rows"].get(table, 0)
    if not partners or not rows:
        return None
    return min(filtered_rows(p, info, stats) for p in partners) / rows


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


def action_key(c, info, stats, pairs=()):
    """Kind of fix: role pattern of its columns, selectivity, driving rows (join indexes
    only), width, table size."""
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
    drive = driving_ratio(table, cols[0], info, stats, pairs) if roles[0] & JOIN_ROLES else None
    return ("index", pattern, "sel=" + sel_bucket(sel if known else None), drive_bucket(drive),
            "width=" + str(len(cols)), "size=" + size_bucket(rows))


JOIN_ROLES = {"join", "correlated", "not_in"}


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
        # Six levels of the lookup table, most specific first: [sum of rewards, tries]
        self.levels = [defaultdict(lambda: [0.0, 0]) for _ in range(6)]
        self.seen_states = set()     # query shapes met in training (for the novelty tag)

    @staticmethod
    def _keys(s, a):
        """Keys from most specific to most general (see FALLBACK at the top).
        Level 1 = this kind of query + this exact fix kind; level 2 = this exact fix kind
        on any query; levels 3-5 drop details in the order FALLBACK says."""
        kind, pattern, size = a[0], a[1], a[-1]
        sel = next((x for x in a if x.startswith("sel=")), "sel=n/a")
        drive = next((x for x in a if x.startswith("drive=")), "drive=n/a")
        if drive != "drive=n/a":
            # join-column index: HOW MANY rows drive the lookup matters more than table size
            return [(s, a), a, (kind, sel, drive, size), (kind, drive, size), (kind, drive), (kind,)]
        if FALLBACK == "pattern-first":
            general = [(kind, pattern, size), (kind, pattern), (kind,)]
        else:
            general = [(kind, sel, size), (kind, size), (kind,)]
        return [(s, a), a] + general

    def score(self, s, a):
        """Estimated reward. Start from the most general level and refine it with each
        more specific level, in proportion to how many real tries that level has."""
        k = BACKOFF_STRENGTH
        est = 0.0
        for table, key in reversed(list(zip(self.levels, self._keys(s, a)))):
            total, n = table[key]
            est = (total + k * est) / (n + k)
        return est

    def update(self, s, a, reward):
        self.seen_states.add(s)
        for table, key in zip(self.levels, self._keys(s, a)):
            table[key][0] += reward
            table[key][1] += 1

    def _order(self, s, entry):
        """Sort key: highest score first; on a tie, the CHEAPER fix (lower write penalty)."""
        c, a = entry
        return (-round(self.score(s, a), 6), write_width(c))

    def rank(self, masked_sql, stats, column_types=None):
        """All candidates, best predicted first: [(score, candidate, action_key)]."""
        info = sqlinfo.analyze(masked_sql, column_types)
        s = state_key(info, stats)
        cands = cand_mod.candidates(masked_sql, column_types, stats.get("existing_indexes"))
        pairs = sqlinfo.join_pairs(masked_sql)
        entries = [(c, action_key(c, info, stats, pairs)) for c in cands]
        entries.sort(key=lambda e: self._order(s, e))      # stable: list order only if fully tied
        return s, [(self.score(s, a), c, a) for c, a in entries]

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
                _, c, a = min(ranked, key=lambda x: self._order(s, (x[1], x[2])))
            key = (i, tuple(c["sqls"]))
            if key not in cache:
                cache[key] = evaluate(sql, c, cost_fn)[2]
            self.update(s, a, cache[key])
        return len(cache)

    def choose(self, masked_sql, stats, column_types=None):
        """Best predicted fix WITHOUT calling cost_fn (the point of learning)."""
        _, ranked = self.rank(masked_sql, stats, column_types)
        return ranked[0][1] if ranked else None


    def choose_verified(self, masked_sql, stats, cost_fn, k=VERIFY_TOP_K, column_types=None):
        """Rank all fixes, then SIMULATE only the top k and return the one with the best real
        reward. Returns (candidate, cost_before, cost_after, reward, n_cost_fn_calls)."""
        _, ranked = self.rank(masked_sql, stats, column_types)
        if not ranked:
            return None, None, None, None, 0
        before = cost_fn(masked_sql, [])
        tried = [(c, *evaluate(masked_sql, c, cost_fn, before)[1:]) for _, c, _ in ranked[:max(1, k)]]
        best = max(tried, key=lambda t: (t[2], -write_width(t[0])))
        return best[0], before, best[1], best[2], 1 + len(tried)

    # --- persistence: the pipeline loads a trained agent instead of retraining ---
    def save(self, path):
        data = {"levels": [[[repr(k), v[0], v[1]] for k, v in lv.items()] for lv in self.levels],
                "seen_states": [repr(x) for x in sorted(self.seen_states)]}
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def load(cls, path):
        agent = cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for lv, rows in zip(agent.levels, data["levels"]):
            for k, total, n in rows:
                lv[ast.literal_eval(k)] = [total, n]
        agent.seen_states = {ast.literal_eval(x) for x in data["seen_states"]}
        return agent


# ------------------------------------------------------------
# Baselines for comparison
# ------------------------------------------------------------
def index_every_filter_column(masked_sql, existing_indexes=None):
    """Naive DBA move: one single-column index on every filter / join column
    (skipping ones that already exist, so it isn't charged for no-op indexes)."""
    existing = cand_mod.normalize_existing(existing_indexes)
    info = sqlinfo.analyze(masked_sql)
    useful = {"eq_filter", "range_filter", "or_filter", "join", "correlated", "not_in"}
    sqls, cols = [], {}
    for key, v in info["columns"].items():
        t, c = key.split(".", 1)
        if t != sqlinfo.UNKNOWN_TABLE and useful & set(v["roles"]) and (t, (c,)) not in existing:
            sqls.append(f"CREATE INDEX ON {t} ({c})")
            cols.setdefault(t, []).append(c)
    return {"kind": "index", "sqls": sqls, "cols": cols, "why": "baseline"}


AGENT_PATH = Path(__file__).with_name("rl_agent.json")
NOVELTY_SEEN = "seen query shape"
NOVELTY_UNSEEN = "unseen shape (lower confidence)"


def load_agent():
    """The trained agent if one was saved, otherwise an untrained one (then every query
    is 'unseen' and the ranking relies on the top-k simulation)."""
    return BanditAgent.load(AGENT_PATH) if AGENT_PATH.exists() else BanditAgent()


def stats_from_plan(masked_plan):
    """Rough stats when the caller has none: rows per table from the plan's scan
    estimates (these are AFTER filters, so tables look smaller than they are).
    The pipeline should pass real masked stats (table sizes, distinct counts)."""
    rows = {}

    def walk(n):
        rel = n.get("Relation Name")
        if rel:
            rows[rel] = max(rows.get(rel, 0), n.get("Plan Rows", 0))
        for k in n.get("Plans", []):
            walk(k)

    if masked_plan and "Plan" in masked_plan:
        walk(masked_plan["Plan"])
    return {"rows": rows, "eq_sel": {}, "range_sel": {}, "existing_indexes": []}


def recommend(masked_sql, masked_plan, cost_fn, stats=None, time_fn=None, agent=None,
              column_types=None, k=VERIFY_TOP_K):
    """Ranked, verified recommendations for one masked query (CLAUDE.md contract).

    Contract keys per item: kind, action, cost_before, cost_after, reason.
    Added keys: improvement_pct, reward, write_penalty, verified_by
    ("hypopg_cost" | "timed_run" | "rule_only"), novelty, agent_score, and for
    rewrites same_results / time_before_ms / time_after_ms / speedup / verify_note.
    The first item also carries why_not: one sentence on the best REJECTED option.
    Optional: stats (masked table stats), time_fn (see ai/verify.py), agent."""
    agent = agent or load_agent()
    stats = stats or stats_from_plan(masked_plan)
    try:
        info = sqlinfo.analyze(masked_sql, column_types)
    except sqlinfo.sqlglot.errors.ParseError:
        # e.g. the privacy STUB returns "SELECT ..." - say so instead of crashing the pipeline
        before = cost_fn(masked_sql, [])
        return [{"kind": "none", "action": "", "cost_before": before, "cost_after": before,
                 "improvement_pct": 0.0, "reward": None, "write_penalty": 0.0, "verified_by": "rule_only",
                 "novelty": NOVELTY_UNSEEN, "agent_score": None,
                 "reason": "The masked SQL could not be parsed, so no fix was analysed."}]
    pairs = sqlinfo.join_pairs(masked_sql)
    s, ranked = agent.rank(masked_sql, stats, column_types)
    if not ranked:
        return []
    novelty = NOVELTY_SEEN if s in agent.seen_states else NOVELTY_UNSEEN
    before = cost_fn(masked_sql, [])

    tried = []
    for score, c, _ in ranked[:max(1, k)]:
        rec = {"kind": c["kind"], "action": "; ".join(c["sqls"]) if c["kind"] != "rewrite" else c["sqls"][0],
               "cost_before": before, "novelty": novelty, "agent_score": round(score, 3),
               "write_penalty": round(WRITE_PENALTY_PER_COL * write_width(c), 3)}
        measured = None
        if c["kind"] == "rewrite":
            v = verify.verify_rewrite(masked_sql, c["sqls"][0], time_fn)
            after = cost_fn(c["sqls"][0], [])
            if v["verified_by"] == "timed_run":
                drop = (1 - 1 / v["speedup"]) if v["same_results"] else -1.0
            else:
                drop = max(-1.0, (before - after) / before) if before else 0.0
            rec.update({"cost_after": after, "verified_by": v["verified_by"], "same_results": v["same_results"],
                        "time_before_ms": v["time_before_ms"], "time_after_ms": v["time_after_ms"],
                        "speedup": v["speedup"], "verify_note": v["note"]})
            reward = drop
            measured = v
            rec["reason"] = f"{c['why']} {v['note'].capitalize()}."
            if v["verified_by"] == "rule_only":
                rec["reason"] += f" Planner estimate: {before:,.0f} -> {after:,.0f}."
        else:
            try:
                _, after, reward = evaluate(masked_sql, c, cost_fn, before)
                drop = max(0.0, (before - after) / before) if before else 0.0
                rec["verified_by"] = "hypopg_cost"
                rec["reason"] = (f"{c['why'][:1].upper()}{c['why'][1:]}: simulated cost {before:,.0f} -> "
                                 f"{after:,.0f} ({100 * drop:.1f}% lower).")
            except ValueError:          # cost_fn can't simulate this kind (e.g. partitions today)
                after, drop, reward = before, 0.0, None
                rec["verified_by"] = "rule_only"
                rec["reason"] = f"{c['why'][:1].upper()}{c['why'][1:]}. Not simulated (no sandbox support yet)."
            rec["cost_after"] = after
            measured = None if reward is None else {"drop": drop, "reward": reward}   # None = not simulated
        rec["improvement_pct"] = round(100 * drop, 1)
        rec["reward"] = None if reward is None else round(reward, 3)
        tried.append((rec, c, measured))

    def sort_key(t):
        rec = t[0]
        rejected = rec.get("same_results") is False
        return (rejected, rec["reward"] is None, -(rec["reward"] or 0.0), rec["write_penalty"])

    tried.sort(key=sort_key)
    # Recommend only what helps: drop wrong-result rewrites and anything measured as no better.
    # Unsimulated (rule_only) options stay, clearly flagged, so the admin still sees them.
    recs = [t[0] for t in tried if t[0].get("same_results") is not False
            and (t[0]["verified_by"] == "rule_only" or t[0]["improvement_pct"] > 0)]
    if not recs:
        return []
    # best rejected = the next-best option we tried, else the top one we never simulated
    chosen_drop = recs[0]["improvement_pct"] / 100
    rejected = [t for t in tried if t[0] is not recs[0]]
    if rejected:
        _, c, measured = rejected[0]
    else:
        c, measured = ranked[k][1] if len(ranked) > k else None, None
    if c is not None:
        chosen = None if recs[0]["verified_by"] == "rule_only" else {"drop": chosen_drop, "label": recs[0]["action"]}
        recs[0]["why_not"] = why_not.why_not(c, info, stats, measured, chosen, pairs)
    return recs


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
          f"{'random':>6} {'pen':>5} | {'index-all':>9} {'pen':>5} | {'verify%':>7} | {'best%':>6}")
    totals = defaultdict(list)
    n_calls = []
    for v in test:
        stats, f = fake_env.stats_for_agent(v["catalog"]), fake_env.make_cost_fn(v["catalog"])
        before = f(v["sql"], [])
        cands = cand_mod.candidates(v["sql"], None, stats["existing_indexes"])
        rl = row(v, agent.choose(v["sql"], stats), before, f)
        verified, _, _, _, calls = agent.choose_verified(v["sql"], stats, f)
        rlv = row(v, verified, before, f)
        n_calls.append(calls)
        rnd = [row(v, c, before, f) for c in cands]
        rnd = tuple(sum(x[i] for x in rnd) / len(rnd) for i in range(3))   # expected value of a random pick
        allx = row(v, index_every_filter_column(v["sql"], stats["existing_indexes"]), before, f)
        best = max(row(v, c, before, f) for c in cands)                   # oracle: tries every fix
        pick = agent.choose(v["sql"], stats)
        label = pick["rule"] if pick["kind"] == "rewrite" else " + ".join(pick["sqls"])
        print(f"{v['id']:7} | {label[:44]:44} {rl[0]:6.1f} {rl[1]:5.2f} | {rnd[0]:6.1f} {rnd[1]:5.2f} | "
              f"{allx[0]:9.1f} {allx[1]:5.2f} | {rlv[0]:7.1f} | {best[0]:6.1f}")
        for name, r in (("RL", rl), (f"RL + top-{VERIFY_TOP_K} verify", rlv), ("random", rnd),
                        ("index-all", allx), ("oracle", best)):
            totals[name].append(r)
    print("\nAVERAGE over held-out queries:")
    print(f"  {'method':18} {'cost drop %':>12} {'write pen':>10} {'reward':>8}")
    for name, rs in totals.items():
        avg = [sum(r[i] for r in rs) / len(rs) for i in range(3)]
        print(f"  {name:18} {avg[0]:12.1f} {avg[1]:10.2f} {avg[2]:8.3f}")
    cands_per_q = [len(cand_mod.candidates(v["sql"], None, v["catalog"]["existing_indexes"])) for v in test]
    print(f"\ncost_fn calls per query: RL alone 0 | RL + top-{VERIFY_TOP_K} verify {max(n_calls)} "
          f"(1 baseline + {VERIFY_TOP_K} fixes) | oracle 1 + all candidates = "
          f"{min(cands_per_q) + 1}-{max(cands_per_q) + 1}")
