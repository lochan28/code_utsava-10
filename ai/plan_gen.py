"""Synthetic MASKED query plans in the real EXPLAIN (FORMAT JSON) structure.

Owner: Person 3.  Dev tool for training the GNN until enough real plans exist.
Every plan uses masked names only (t_xxx tables, c_xxx columns, $n values).

Each plan gets a LABEL: the id of its bottleneck node. The default rule is
"highest self cost" (a node's cost minus its children's). Three HAND OVERRIDES
fix cases where that rule is known to be wrong - they are listed in
OVERRIDES below and applied by name, so they are easy to see and change:

  repeated_inner   Nested Loop with a Seq Scan inside: the scan is costed ONCE in
                   EXPLAIN but really runs once per outer row, so the Nested Loop
                   node looks expensive while the repeated scan is the real culprit.
  correlated_sub   Same idea for a correlated SubPlan: its scan re-runs per outer row.
  misestimate      (only plans with actual row counts) a scan whose real row count is
                   >= 50x the planner's estimate: every cost above it is wrong, so
                   the badly estimated scan is where to look.

Self-test:  .venv/Scripts/python -m ai.plan_gen
"""
import hashlib
import math
import random

# ============================================================
# TUNABLE SETTINGS
# ============================================================
SEED = 11
PLANS_PER_SHAPE = 18          # 17 shapes x 18 = 306 plans
TEST_SHAPES = {"hash_join3", "nl_seq_inner_agg", "merge_join", "bitmap",
               "gather_merge_sort", "correlated_sub_agg"}             # held out whole
WITH_ACTUALS_P = 0.4          # Share of plans that also carry actual row counts (EXPLAIN ANALYZE)
MISESTIMATE_P = 0.35          # Of those, share where one scan is badly misestimated
MISESTIMATE_RANGE = (50, 2000)  # Actual rows = estimate x this factor

# ============================================================
# FIXED SETTINGS
# ============================================================
OVERRIDES = ("repeated_inner", "correlated_sub", "misestimate")
MISESTIMATE_THRESHOLD = 50


class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.n = 0

    def name(self, prefix):
        self.n += 1
        return f"{prefix}_{hashlib.sha256(f'{self.rng.random()}{self.n}'.encode()).hexdigest()[:3]}"

    # --- leaf scans ---------------------------------------------------------
    def seq_scan(self, table_rows, sel, parallel=False):
        rows = max(1, int(table_rows * sel))
        cost = table_rows * 0.02 / (3 if parallel else 1)
        return {"Node Type": "Seq Scan", "Parallel Aware": parallel, "Relation Name": self.name("t"),
                "Startup Cost": 0.0, "Total Cost": round(cost, 2), "Plan Rows": rows,
                "Filter": f"({self.name('c')} = $1)" if sel < 1 else None, "_table_rows": table_rows}

    def index_scan(self, table_rows, rows):
        cost = math.log2(table_rows + 2) * 0.5 + rows * 0.6
        return {"Node Type": "Index Scan", "Relation Name": self.name("t"), "Index Name": self.name("i"),
                "Startup Cost": 0.4, "Total Cost": round(cost, 2), "Plan Rows": max(1, int(rows)),
                "Index Cond": f"({self.name('c')} = $1)", "_table_rows": table_rows}

    # --- inner nodes --------------------------------------------------------
    @staticmethod
    def wrap(node_type, children, own_cost, rows, rels=None, **extra):
        total = sum(c["Total Cost"] for c in children) + own_cost
        for c, r in zip(children, rels or [None] * len(children)):
            if r:
                c["Parent Relationship"] = r
        return {"Node Type": node_type, "Startup Cost": 0.0, "Total Cost": round(total, 2),
                "Plan Rows": max(1, int(rows)), "Plans": children, **extra}

    def hash(self, child):
        return self.wrap("Hash", [child], child["Plan Rows"] * 0.02, child["Plan Rows"], ["Outer"])

    def hash_join(self, outer, inner, rows):
        h = self.hash(inner)
        return self.wrap("Hash Join", [outer, h], (outer["Plan Rows"] + rows) * 0.01, rows, ["Outer", "Inner"],
                         **{"Hash Cond": f"({self.name('c')} = {self.name('c')})"})

    def nested_loop(self, outer, inner, rows):
        # EXPLAIN shows the inner child's cost for ONE loop; the join pays it once per outer row
        repeated = outer["Plan Rows"] * inner["Total Cost"]
        node = self.wrap("Nested Loop", [outer, inner], rows * 0.01, rows, ["Outer", "Inner"])
        node["Total Cost"] = round(outer["Total Cost"] + repeated + rows * 0.01, 2)
        return node

    def sort(self, child):
        r = child["Plan Rows"]
        return self.wrap("Sort", [child], r * math.log2(r + 2) * 0.02, r, ["Outer"],
                         **{"Sort Key": [self.name("c")]})

    def aggregate(self, child, groups, strategy="Hashed", node_type="Aggregate"):
        return self.wrap(node_type, [child], child["Plan Rows"] * 0.01, groups, ["Outer"], Strategy=strategy)


def _annotate(root):
    """Pre-order ids, parent links and self cost (cost minus children's cost)."""
    nodes = []

    def walk(n, parent):
        n["_id"] = len(nodes)
        n["_parent"] = parent
        nodes.append(n)
        for c in n.get("Plans", []):
            walk(c, n["_id"])

    walk(root, None)
    for n in nodes:
        n["_self_cost"] = max(0.0, n["Total Cost"] - sum(c["Total Cost"] for c in n.get("Plans", [])))
    return nodes


# ------------------------------------------------------------
# Plan shapes
# ------------------------------------------------------------
def make_shape(shape, g, r):
    big = int(10 ** r.uniform(5, 7.3))      # 100k .. 20M
    mid = int(10 ** r.uniform(3.5, 5))
    small = int(10 ** r.uniform(1, 3.5))
    sel = 10 ** r.uniform(-4, -0.3)
    override = None
    if shape == "seq_filter":
        root = g.aggregate(g.seq_scan(big, sel), 1, "Plain")
    elif shape == "sort_scan":
        root = g.sort(g.seq_scan(big, sel))
    elif shape == "hash_join2":
        j = g.hash_join(g.seq_scan(big, sel), g.seq_scan(small, 1.0), big * sel)
        root = g.aggregate(j, small)
    elif shape == "hash_join3":
        j1 = g.hash_join(g.seq_scan(big, sel), g.seq_scan(mid, 1.0), big * sel)
        j2 = g.hash_join(j1, g.seq_scan(small, 1.0), big * sel)
        root = g.sort(g.aggregate(j2, small * 5))
    elif shape == "nl_index":
        outer = g.seq_scan(mid, 10 ** r.uniform(-3, -1))
        inner = g.index_scan(big, r.uniform(1, 20))
        root = g.nested_loop(outer, inner, outer["Plan Rows"] * inner["Plan Rows"])
    elif shape == "nl_seq_inner":
        outer = g.seq_scan(mid, 10 ** r.uniform(-2.5, -1))
        inner = g.seq_scan(mid, 10 ** r.uniform(-3, -1.5))
        root = g.nested_loop(outer, inner, outer["Plan Rows"])
        override = ("repeated_inner", inner)
    elif shape == "nl_seq_inner_agg":
        outer = g.hash_join(g.seq_scan(mid, 10 ** r.uniform(-2.5, -1)), g.seq_scan(small, 1.0), mid * 0.01)
        inner = g.seq_scan(mid, 10 ** r.uniform(-3, -1.5))
        nl = g.nested_loop(outer, inner, outer["Plan Rows"])
        root = g.sort(g.aggregate(nl, small))
        override = ("repeated_inner", inner)
    elif shape == "correlated_sub":
        outer = g.seq_scan(mid, 10 ** r.uniform(-3, -1.5))
        sub_scan = g.seq_scan(big, 10 ** r.uniform(-5, -3))
        sub = g.aggregate(sub_scan, 1, "Plain")
        sub["Parent Relationship"] = "SubPlan"
        sub["Subplan Name"] = "SubPlan 1"
        outer["Plans"] = [sub]
        outer["Total Cost"] = round(outer["Total Cost"] + outer["Plan Rows"] * sub["Total Cost"], 2)
        root = outer
        override = ("correlated_sub", sub_scan)
    elif shape == "merge_join":
        a, b = g.sort(g.seq_scan(big, sel)), g.sort(g.seq_scan(mid, 1.0))
        root = g.wrap("Merge Join", [a, b], (a["Plan Rows"] + b["Plan Rows"]) * 0.01, a["Plan Rows"],
                      ["Outer", "Inner"])
    elif shape == "parallel":
        scan = g.seq_scan(big, sel, parallel=True)
        partial = g.aggregate(scan, 1, "Plain", "Aggregate")
        partial["Partial Mode"] = "Partial"
        gather = g.wrap("Gather", [partial], 1000.0, 3, ["Outer"], **{"Workers Planned": 2})
        root = g.aggregate(gather, 1, "Plain")
        root["Partial Mode"] = "Finalize"
    elif shape == "bitmap":
        rows = int(big * 10 ** r.uniform(-4, -2))
        bix = {"Node Type": "Bitmap Index Scan", "Index Name": g.name("i"), "Startup Cost": 0.0,
               "Total Cost": round(rows * 0.005 + 5, 2), "Plan Rows": rows, "Parent Relationship": "Outer",
               "Index Cond": f"({g.name('c')} = $1)"}
        heap = g.wrap("Bitmap Heap Scan", [bix], rows * 0.4, rows, **{"Relation Name": g.name("t"),
                                                                       "_table_rows": big})
        root = g.aggregate(heap, 1, "Plain")
    elif shape == "limit_sort":
        root = g.wrap("Limit", [g.sort(g.seq_scan(big, sel))], 0.01, 10, ["Outer"])
    elif shape == "index_only_agg":
        rows = int(big * 10 ** r.uniform(-4, -1))
        io = {"Node Type": "Index Only Scan", "Relation Name": g.name("t"), "Index Name": g.name("i"),
              "Startup Cost": 0.4, "Total Cost": round(rows * 0.05 + 5, 2), "Plan Rows": rows,
              "Index Cond": f"({g.name('c')} >= $1)"}
        root = g.aggregate(io, int(10 ** r.uniform(0, 3)))
    elif shape == "gather_merge_sort":
        scan = g.seq_scan(big, sel, parallel=True)
        srt = g.sort(scan)
        root = g.wrap("Gather Merge", [srt], srt["Plan Rows"] * 0.02, srt["Plan Rows"] * 3, ["Outer"],
                      **{"Workers Planned": 2})
    elif shape == "materialize_nl":
        outer = g.seq_scan(mid, 10 ** r.uniform(-2, -0.5))
        inner = g.seq_scan(small, 1.0)
        mat = g.wrap("Materialize", [inner], inner["Plan Rows"] * 0.01, inner["Plan Rows"], ["Outer"])
        mat["Parent Relationship"] = "Inner"
        # Materialize: inner is computed ONCE, then re-read cheaply - so NOT a repeated-scan case
        pairs = outer["Plan Rows"] * inner["Plan Rows"]
        root = {"Node Type": "Nested Loop", "Startup Cost": 0.0, "Plan Rows": max(1, int(pairs * 0.01)),
                "Total Cost": round(outer["Total Cost"] + mat["Total Cost"] + pairs * 0.0025, 2),
                "Plans": [outer, mat], "Join Filter": f"({g.name('c')} = {g.name('c')})"}
        outer["Parent Relationship"] = "Outer"
    elif shape == "nl_seq_inner_join":
        outer = g.seq_scan(mid, 10 ** r.uniform(-2.5, -1))
        inner = g.seq_scan(mid, 10 ** r.uniform(-3, -1.5))
        nl = g.nested_loop(outer, inner, outer["Plan Rows"])
        root = g.hash_join(g.seq_scan(big, sel), nl, big * sel)
        override = ("repeated_inner", inner)
    elif shape == "correlated_sub_agg":
        outer = g.seq_scan(mid, 10 ** r.uniform(-3, -1.5))
        sub_scan = g.seq_scan(big, 10 ** r.uniform(-5, -3))
        sub = g.aggregate(sub_scan, 1, "Plain")
        sub["Parent Relationship"] = "SubPlan"
        sub["Subplan Name"] = "SubPlan 1"
        outer["Plans"] = [sub]
        outer["Total Cost"] = round(outer["Total Cost"] + outer["Plan Rows"] * sub["Total Cost"], 2)
        root = g.sort(g.aggregate(outer, int(outer["Plan Rows"] / 3) + 1))
        override = ("correlated_sub", sub_scan)
    else:
        raise ValueError(shape)
    return root, override


SHAPES = ["seq_filter", "sort_scan", "hash_join2", "hash_join3", "nl_index", "nl_seq_inner",
          "nl_seq_inner_agg", "correlated_sub", "merge_join", "parallel", "bitmap",
          "limit_sort", "index_only_agg", "gather_merge_sort", "materialize_nl", "nl_seq_inner_join",
          "correlated_sub_agg"]


def make_plans(seed=SEED):
    """[{"id", "shape", "plan": {"Plan": root}, "label": node_id, "label_rule": str}]."""
    r = random.Random(seed)
    out = []
    for shape in SHAPES:
        for k in range(PLANS_PER_SHAPE):
            g = Gen(r)
            root, override = make_shape(shape, g, r)
            nodes = _annotate(root)
            label = max(nodes, key=lambda n: n["_self_cost"])["_id"]
            rule = "highest_self_cost"
            if override:
                rule, label = override[0], override[1]["_id"]
            if r.random() < WITH_ACTUALS_P:
                for n in nodes:                       # actual rows = estimate with mild noise
                    n["Actual Rows"] = max(0, int(n["Plan Rows"] * 10 ** r.uniform(-0.3, 0.3)))
                    n["Actual Loops"] = 1
                scans = [n for n in nodes if "Scan" in n["Node Type"]]
                if scans and r.random() < MISESTIMATE_P:
                    bad = r.choice(scans)
                    bad["Actual Rows"] = int(bad["Plan Rows"] * r.uniform(*MISESTIMATE_RANGE))
                    rule, label = "misestimate", bad["_id"]
            for n in nodes:                            # drop private helper fields
                for key in [k for k in n if k.startswith("_")]:
                    del n[key]
            out.append({"id": f"{shape}-{k}", "shape": shape, "plan": {"Plan": root},
                        "label": label, "label_rule": rule})
    return out


def split(plans):
    return ([p for p in plans if p["shape"] not in TEST_SHAPES],
            [p for p in plans if p["shape"] in TEST_SHAPES])


if __name__ == "__main__":
    import json
    from collections import Counter

    plans = make_plans()
    train, test = split(plans)
    print(f"{len(plans)} plans: {len(train)} train / {len(test)} test (held-out shapes {sorted(TEST_SHAPES)})")
    print("label rules, train:", dict(Counter(p["label_rule"] for p in train)))
    print("label rules, test: ", dict(Counter(p["label_rule"] for p in test)))
    text = json.dumps([p["plan"] for p in plans])
    print("only masked names:", all(w[:2] in ("t_", "c_", "i_") for w in __import__("re").findall(r"\b[a-z]_[0-9a-f]{3}\b", text)))
    ex = next(p for p in plans if p["shape"] == "nl_seq_inner")
    print("\nexample (nl_seq_inner), label node", ex["label"], ex["label_rule"])
    print(json.dumps(ex["plan"], indent=1)[:900])
