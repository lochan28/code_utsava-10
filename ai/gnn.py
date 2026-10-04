"""GNN over the (masked) plan tree that flags the bottleneck node.

Owner: Person 3.  Sees ONLY masked data (EXPLAIN JSON with t_xxx / c_xxx names).

How it works, simply:
  - Every step of the plan (scan, join, sort, ...) becomes a node in a graph;
    parent and child steps are connected.
  - Each node gets numbers describing it: what kind of step it is, how many rows,
    its total cost, its OWN cost (minus its children), and - when the plan was
    run with ANALYZE - how wrong the planner's row estimate was.
  - Score of a node = the BASELINE score (its share of the plan's self cost) plus a
    small CORRECTION from a 2-layer graph network (GCN) that looks at the node's
    neighbours. The correction starts at zero and is penalised, so on ordinary plans
    the answer stays "highest self cost"; it only moves for repeated inner scans and
    misestimated rows.
  - Three paths, always reported in the output ("path"):
      gnn           the model passed validation (held-out normal plans >= baseline)
      gated_hybrid  model not validated: baseline, except the GNN may pick a node the
                    plain rules flag as special, if it is >= GATE_CONFIDENCE sure
      rules         no trained weights: baseline + two plain if-check overrides
  - The reason sentence is built from the evidence numbers, no LLM.

Baseline it must match: "pick the node with the highest self cost".

Self-test (train on synthetic plans, test on held-out plan shapes, save weights):
    .venv/Scripts/python -m ai.gnn
"""
import math
import time
from pathlib import Path

import torch
from torch_geometric.data import Batch, Data
from torch_geometric.nn import GCNConv
from torch_geometric.utils import softmax

# ============================================================
# TUNABLE SETTINGS
# ============================================================
SEED = 11
HIDDEN = 32            # Width of the hidden layers. Bigger = can learn more patterns,
                       # but needs more plans to avoid memorising. 16-64 is plenty here.
EPOCHS = 300           # Training passes over all training plans. ~seconds on GPU.
LEARNING_RATE = 0.01
BATCH_SIZE = 32
WEIGHT_DECAY = 1e-4    # Small penalty on big weights; helps it generalise to unseen shapes.
MISESTIMATE_FLAG = 50  # Rows actual/estimate >= this counts as a badly misestimated step
                       # (used by the reason text and by the plain-rule override).
CORRECTION_L2 = 0.01   # Penalty on the GNN's correction to the baseline score. Higher = sticks
                       # closer to "highest self cost"; 0 = free to override anything.
BOTTLENECK_PATH = "rules"  # Which path find_bottleneck() uses:
                           #   "rules"        baseline + two plain if-checks (no learning) - DEFAULT
                           #   "gated_hybrid" baseline; GNN may override rule-flagged nodes if sure
                           #   "gnn"          the trained model alone
                           #   "auto"         "gnn" if the saved weights are VALIDATED, else "gated_hybrid"
                           # Paths needing weights fall back to "rules" when no weights file exists.
GATE_CONFIDENCE = 0.8  # Gated hybrid: the GNN may override the baseline only when it is at
                       # least this sure AND its pick is a rule-flagged special case.

# ============================================================
# FIXED SETTINGS - feature layout (changing these invalidates saved weights)
# ============================================================
OPS = ["Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan",
       "Nested Loop", "Hash Join", "Merge Join", "Hash", "Sort", "Aggregate", "Gather",
       "Gather Merge", "Limit", "Materialize"]                    # anything else -> "other"
RELATIONS = ["Outer", "Inner", "SubPlan"]                         # anything else -> "other"
N_FEATURES = len(OPS) + 1 + len(RELATIONS) + 1 + 6
WEIGHTS_PATH = Path(__file__).with_name("gnn_weights.pt")
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# ------------------------------------------------------------
# Plan tree -> flat node list with evidence numbers
# ------------------------------------------------------------
def flatten(masked_plan):
    """Pre-order list of nodes with parent id, naive self cost, repeat count (loops)
    and the effective cost share each node really contributes."""
    nodes = []

    def walk(n, parent, loops):
        i = len(nodes)
        kids = n.get("Plans", [])
        naive_self = max(0.0, n["Total Cost"] - sum(k["Total Cost"] for k in kids))
        # what this node itself adds, once repeated children are counted where they belong
        repeated = 0.0
        outer_rows = next((k["Plan Rows"] for k in kids if k.get("Parent Relationship") == "Outer"), 1)
        for k in kids:
            if (n["Node Type"] == "Nested Loop" and k.get("Parent Relationship") == "Inner"
                    and k["Node Type"] != "Materialize"):      # Materialize: computed once, re-read
                repeated += (outer_rows - 1) * k["Total Cost"]
            elif k.get("Parent Relationship") == "SubPlan":
                repeated += (n["Plan Rows"] - 1) * k["Total Cost"]
        nodes.append({"id": i, "parent": parent, "node": n, "naive_self": naive_self,
                      "true_self": max(0.0, naive_self - repeated), "loops": loops})
        for k in kids:
            rel = k.get("Parent Relationship")
            if n["Node Type"] == "Nested Loop" and rel == "Inner" and k["Node Type"] != "Materialize":
                k_loops = loops * max(1, outer_rows)
            elif rel == "SubPlan":
                k_loops = loops * max(1, n["Plan Rows"])
            else:
                k_loops = loops
            walk(k, i, k_loops)

    walk(masked_plan["Plan"], None, 1)
    root_cost = max(1e-9, masked_plan["Plan"]["Total Cost"])
    for x in nodes:
        x["share"] = min(1.0, x["true_self"] * x["loops"] / root_cost)
        n = x["node"]
        x["ratio"] = (n["Actual Rows"] / max(1, n["Plan Rows"])) if "Actual Rows" in n else None
    return nodes


def baseline(masked_plan):
    """'Pick the node with the highest self cost' (cost minus its children)."""
    return max(flatten(masked_plan), key=lambda x: x["naive_self"])["id"]


def to_graph(masked_plan, label=None):
    nodes = flatten(masked_plan)
    root_cost = max(1.0, masked_plan["Plan"]["Total Cost"])
    feats = []
    for x in nodes:
        n = x["node"]
        op = [float(n["Node Type"] == o) for o in OPS] + [float(n["Node Type"] not in OPS)]
        rel = n.get("Parent Relationship")
        rl = [float(rel == r) for r in RELATIONS] + [float(rel not in RELATIONS)]
        ratio = x["ratio"]
        feats.append(op + rl + [
            math.log1p(n["Plan Rows"]) / 15,
            math.log1p(n["Total Cost"]) / 15,
            math.log1p(x["naive_self"]) / 15,
            x["naive_self"] / root_cost,
            float(ratio is not None),
            max(-3.0, min(3.0, math.log10(max(ratio, 1e-3)))) / 3 if ratio is not None else 0.0,
        ])
    edges = [[x["id"], x["parent"]] for x in nodes if x["parent"] is not None]
    edge_index = torch.tensor(edges + [[b, a] for a, b in edges], dtype=torch.long).t().contiguous() \
        if edges else torch.zeros((2, 0), dtype=torch.long)
    share = [min(1 - 1e-6, max(1e-6, x["naive_self"] / root_cost)) for x in nodes]
    data = Data(x=torch.tensor(feats, dtype=torch.float), edge_index=edge_index,
                base=torch.tensor([math.log(v / (1 - v)) for v in share], dtype=torch.float))
    if label is not None:
        data.y = torch.tensor([label])
    return data


# ------------------------------------------------------------
# The model
# ------------------------------------------------------------
class BottleneckGCN(torch.nn.Module):
    """score = logit(self-cost share)  [the baseline]  +  correction  [the GCN]."""

    def __init__(self, hidden=HIDDEN):
        super().__init__()
        self.conv1 = GCNConv(N_FEATURES, hidden)
        self.conv2 = GCNConv(hidden, hidden)
        self.head = torch.nn.Linear(hidden + N_FEATURES, 1)   # also sees the raw features
        torch.nn.init.zeros_(self.head.weight)               # correction starts at exactly 0
        torch.nn.init.zeros_(self.head.bias)

    def correction(self, data):
        h = torch.relu(self.conv1(data.x, data.edge_index))
        h = torch.relu(self.conv2(h, data.edge_index))
        return self.head(torch.cat([h, data.x], dim=1)).squeeze(-1)

    def forward(self, data):
        return data.base + self.correction(data)            # one score per node


def _batch_loss(model, batch):
    corr = model.correction(batch)
    prob = softmax(batch.base + corr, batch.batch)      # softmax within each plan
    target = batch.ptr[:-1] + batch.y                   # label node's index in the batch
    return -torch.log(prob[target] + 1e-9).mean() + CORRECTION_L2 * (corr ** 2).mean()


def train(plans, epochs=EPOCHS, seed=SEED, verbose=False):
    """plans: [{"plan": masked_plan, "label": node_id}]. Returns the trained model."""
    torch.manual_seed(seed)
    gen = torch.Generator().manual_seed(seed)
    graphs = [to_graph(p["plan"], p["label"]) for p in plans]
    model = BottleneckGCN().to(DEVICE)
    opt = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
    model.train()
    for ep in range(epochs):
        order = torch.randperm(len(graphs), generator=gen).tolist()
        total = 0.0
        for i in range(0, len(order), BATCH_SIZE):
            batch = Batch.from_data_list([graphs[j] for j in order[i:i + BATCH_SIZE]]).to(DEVICE)
            opt.zero_grad()
            loss = _batch_loss(model, batch)
            loss.backward()
            opt.step()
            total += loss.item()
        if verbose and (ep + 1) % 100 == 0:
            print(f"  epoch {ep + 1}: loss {total:.3f}")
    model.eval()
    return model


@torch.no_grad()
def predict(model, masked_plan, with_prob=False):
    data = to_graph(masked_plan).to(DEVICE)
    prob = torch.softmax(model(Batch.from_data_list([data])), 0)
    i = int(prob.argmax())
    return (i, float(prob[i])) if with_prob else i


# ------------------------------------------------------------
# Plain rules (no learning): the two known blind spots of the baseline
# ------------------------------------------------------------
def special_nodes(masked_plan):
    """Node ids the plain rules flag: badly misestimated scans, and Seq Scans that
    really run many times (inner of a Nested Loop, or inside a correlated SubPlan)."""
    nodes = flatten(masked_plan)
    mis = [x for x in nodes if "Scan" in x["node"]["Node Type"] and x["ratio"] is not None
           and x["ratio"] >= MISESTIMATE_FLAG]
    repeated = [x for x in nodes if x["node"]["Node Type"] == "Seq Scan" and x["loops"] > 1]
    return mis, repeated


def rules_pick(masked_plan):
    """Baseline + two if-checks. Misestimate wins first (the costs above it are wrong),
    then a repeated Seq Scan if its REAL cost (cost x loops) beats the baseline pick."""
    nodes = flatten(masked_plan)
    mis, repeated = special_nodes(masked_plan)
    if mis:
        return max(mis, key=lambda x: x["ratio"])["id"], "misestimated rows"
    base = baseline(masked_plan)
    if repeated:
        best = max(repeated, key=lambda x: x["true_self"] * x["loops"])
        if best["true_self"] * best["loops"] > nodes[base]["true_self"]:
            return best["id"], "repeated inner scan"
    return base, "highest self cost"


# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------
_model, _meta = None, {}


def save_model(model, validated, metrics):
    """Weights + VALIDATED flag + the numbers that decided it, in one file."""
    torch.save({"state_dict": model.state_dict(), "validated": bool(validated), "metrics": metrics},
               WEIGHTS_PATH)


def load_model():
    global _model, _meta
    if _model is None and WEIGHTS_PATH.exists():
        saved = torch.load(WEIGHTS_PATH, map_location=DEVICE)
        if isinstance(saved, dict) and "state_dict" in saved:
            m = BottleneckGCN().to(DEVICE)
            m.load_state_dict(saved["state_dict"])
            m.eval()
            _model, _meta = m, {"validated": saved["validated"], "metrics": saved["metrics"]}
        # an old weights file without the flag is ignored -> rules path
    return _model, _meta


def explain_node(masked_plan, node_id, source):
    x = flatten(masked_plan)[node_id]
    n = x["node"]
    op, rel, rows = n["Node Type"], n.get("Relation Name") or n.get("Index Name"), n["Plan Rows"]
    where = f" on {rel}" if rel else ""
    share = round(100 * x["share"], 1)
    reason = (f"{op}{where} is the bottleneck: it handles about {rows:,} rows "
              f"and accounts for about {share}% of the plan's estimated cost.")
    if x["loops"] > 1:
        reason += (f" It runs once per row of its outer input (about {x['loops']:,} times), "
                   f"but EXPLAIN shows its cost only once.")
    if x["ratio"] is not None and x["ratio"] >= MISESTIMATE_FLAG:
        reason += (f" The planner expected {rows:,} rows but {n['Actual Rows']:,} came back "
                   f"({x['ratio']:,.0f}x more), so every cost above this step is underestimated.")
    return {
        "node_id": node_id, "operator": op, "relation": rel, "rows": rows,
        "cost_share_pct": share, "reason": reason,
        # contract keys (CLAUDE.md): op + evidence
        "op": op,
        "evidence": {"relation": rel, "plan_rows": rows, "actual_rows": n.get("Actual Rows"),
                     "loops": x["loops"], "self_cost": round(x["naive_self"], 2),
                     "cost_share_pct": share, "picked_by": source},
    }


def choose(masked_plan, model=None, validated=False):
    """(node_id, path) - see the three paths in the module docstring."""
    if model is None:
        return rules_pick(masked_plan)[0], "rules"
    if validated:
        return predict(model, masked_plan), "gnn"
    base = baseline(masked_plan)
    pick, prob = predict(model, masked_plan, with_prob=True)
    mis, repeated = special_nodes(masked_plan)
    if pick != base and pick in {x["id"] for x in mis + repeated} and prob >= GATE_CONFIDENCE:
        return pick, "gated_hybrid"
    return base, "gated_hybrid"


def choose_by_setting(masked_plan, path_setting=None):
    """(node_id, path actually used) for the BOTTLENECK_PATH setting."""
    setting = path_setting or BOTTLENECK_PATH
    if setting == "rules":
        return choose(masked_plan)
    model, meta = load_model()
    if model is None:
        return choose(masked_plan)                       # no weights -> rules
    if setting == "gnn":
        return predict(model, masked_plan), "gnn"
    if setting == "gated_hybrid":
        return choose(masked_plan, model, validated=False)
    return choose(masked_plan, model, meta.get("validated", False))   # "auto"


def find_bottleneck(masked_plan, path_setting=None):
    """Bottleneck node of a masked EXPLAIN plan, with a readable reason and evidence.
    The output says which path picked it ("path": gnn / gated_hybrid / rules)."""
    node_id, path = choose_by_setting(masked_plan, path_setting)
    out = explain_node(masked_plan, node_id, path)
    out["path"] = path
    out["second_opinion"] = so = second_opinion(masked_plan, node_id)
    if so:                                   # GNN-generated reason (PS4), the rules stay the safety check
        out["reason"] = gnn_sentence(masked_plan, so) + " " + out["reason"]
        out["evidence"]["gnn_top3"] = gnn_top(masked_plan, 3)
    return out


@torch.no_grad()
def gnn_top(masked_plan, k=3):
    """The GNN's k highest-rated steps: [{node_id, op, relation, score}] (softmax over steps)."""
    model, _ = load_model()
    prob = torch.softmax(model(Batch.from_data_list([to_graph(masked_plan).to(DEVICE)])), 0).cpu()
    nodes = flatten(masked_plan)
    return [{"node_id": i, "op": nodes[i]["node"]["Node Type"],
             "relation": nodes[i]["node"].get("Relation Name") or nodes[i]["node"].get("Index Name"),
             "score": round(float(prob[i]), 3)} for i in prob.argsort(descending=True)[:k].tolist()]


def gnn_sentence(masked_plan, so):
    """One sentence from the GNN's reading of the plan; says so when the rule check overrides it."""
    where = f" on {so['relation']}" if so["relation"] else ""
    if so["agrees"]:
        return (f"The GNN reads the plan as a graph and rates {so['op']}{where} as the bottleneck "
                f"({so['confidence']:.0%} of its score); the rule check agrees.")
    return (f"The GNN rated {so['op']}{where} highest ({so['confidence']:.0%}), but the rule check "
            f"({rules_pick(masked_plan)[1]}) picked the step below instead.")


def second_opinion(masked_plan, chosen_id):
    """The shipped GNN's own pick, shown NEXT TO the chosen reason (CLAUDE.md v3).
    None when there are no weights. It never changes the chosen bottleneck."""
    model, _ = load_model()
    if model is None:
        return None
    node_id, prob = predict(model, masked_plan, with_prob=True)
    n = flatten(masked_plan)[node_id]["node"]
    return {"node_id": node_id, "op": n["Node Type"],
            "relation": n.get("Relation Name") or n.get("Index Name"),
            "confidence": round(prob, 3), "agrees": node_id == chosen_id}


if __name__ == "__main__":
    from ai import plan_gen

    plans = plan_gen.make_plans()
    tr, te = plan_gen.split(plans)
    print(f"device: {DEVICE} ({torch.cuda.get_device_name(0) if DEVICE.type == 'cuda' else 'cpu'})")
    print(f"{len(tr)} train plans / {len(te)} held-out plans (shapes {sorted(plan_gen.TEST_SHAPES)})")
    t0 = time.time()
    model = train(tr, verbose=True)
    print(f"trained in {time.time() - t0:.1f} s")

    methods = {
        "baseline": lambda p: baseline(p),
        "rules": lambda p: choose(p)[0],
        "gnn (raw)": lambda p: predict(model, p),
        "gated_hybrid": lambda p: choose(p, model, validated=False)[0],
    }
    cats = {"overall": lambda r: True,
            "normal": lambda r: r == "highest_self_cost",
            "repeated inner scan": lambda r: r in ("repeated_inner", "correlated_sub"),
            "misestimated rows": lambda r: r == "misestimate"}

    def table(title, ps):
        res = {m: {} for m in methods}
        for m, f in methods.items():
            for c, keep in cats.items():
                sub = [p for p in ps if keep(p["label_rule"])]
                res[m][c] = (sum(f(p["plan"]) == p["label"] for p in sub), len(sub))
        print(f"\n{title}")
        print(f"  {'method':14}" + "".join(f"{c:>24}" for c in cats))
        for m in methods:
            print(f"  {m:14}" + "".join(f"{f'{k}/{n} ({100 * k / max(1, n):.0f}%)':>24}"
                                        for k, n in res[m].values()))
        return res

    table("TRAIN shapes", tr)
    res = table("HELD-OUT shapes (never trained on)", te)
    g_norm, base_norm = res["gnn (raw)"]["normal"][0], res["baseline"]["normal"][0]
    validated = g_norm >= base_norm
    save_model(model, validated, {"heldout_normal_gnn": g_norm, "heldout_normal_baseline": base_norm,
                                  "heldout_normal_n": res["baseline"]["normal"][1]})
    print(f"\nVALIDATED = {validated}  (held-out normal plans: GNN {g_norm} vs baseline {base_norm})"
          f" -> 'auto' would use: {'gnn' if validated else 'gated_hybrid'}; "
          f"shipped setting BOTTLENECK_PATH = {BOTTLENECK_PATH!r}")

    print("\nEXAMPLE OUTPUTS via find_bottleneck() (held-out plans, weights reloaded from disk):")
    for rule in ("highest_self_cost", "repeated_inner", "misestimate"):
        p = next(p for p in te if p["label_rule"] == rule)
        out = find_bottleneck(p["plan"])
        ok = "correct" if out["node_id"] == p["label"] else f"WRONG (label {p['label']})"
        print(f"\n[{p['id']}] label: {rule} -> node {out['node_id']} ({ok}), path={out['path']}; "
              f"baseline would pick node {baseline(p['plan'])}")
        print({k: out[k] for k in ("node_id", "operator", "relation", "rows", "cost_share_pct", "path")})
        print("reason:", out["reason"])
