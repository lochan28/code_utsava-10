"""GNN over the (masked) plan tree that flags the bottleneck node.

Owner: Person 3.  Sees ONLY masked data (EXPLAIN JSON with t_xxx / c_xxx names).

How it works, simply:
  - Every step of the plan (scan, join, sort, ...) becomes a node in a graph;
    parent and child steps are connected.
  - Each node gets numbers describing it: what kind of step it is, how many rows,
    its total cost, its OWN cost (minus its children), and - when the plan was
    run with ANALYZE - how wrong the planner's row estimate was.
  - A tiny 2-layer graph network (GCN) lets every node "look at" its neighbours
    twice, then scores each node; the highest score is the bottleneck.
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
MISESTIMATE_FLAG = 50  # Reason text mentions the row estimate when actual/estimate >= this.

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
            if n["Node Type"] == "Nested Loop" and k.get("Parent Relationship") == "Inner":
                repeated += (outer_rows - 1) * k["Total Cost"]
            elif k.get("Parent Relationship") == "SubPlan":
                repeated += (n["Plan Rows"] - 1) * k["Total Cost"]
        nodes.append({"id": i, "parent": parent, "node": n, "naive_self": naive_self,
                      "true_self": max(0.0, naive_self - repeated), "loops": loops})
        for k in kids:
            rel = k.get("Parent Relationship")
            if n["Node Type"] == "Nested Loop" and rel == "Inner":
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
    data = Data(x=torch.tensor(feats, dtype=torch.float), edge_index=edge_index)
    if label is not None:
        data.y = torch.tensor([label])
    return data


# ------------------------------------------------------------
# The model
# ------------------------------------------------------------
class BottleneckGCN(torch.nn.Module):
    def __init__(self, hidden=HIDDEN):
        super().__init__()
        self.conv1 = GCNConv(N_FEATURES, hidden)
        self.conv2 = GCNConv(hidden, hidden)
        self.head = torch.nn.Linear(hidden + N_FEATURES, 1)   # also sees the raw features

    def forward(self, data):
        h = torch.relu(self.conv1(data.x, data.edge_index))
        h = torch.relu(self.conv2(h, data.edge_index))
        return self.head(torch.cat([h, data.x], dim=1)).squeeze(-1)   # one score per node


def _batch_loss(model, batch):
    scores = model(batch)
    prob = softmax(scores, batch.batch)                 # softmax within each plan
    target = batch.ptr[:-1] + batch.y                   # label node's index in the batch
    return -torch.log(prob[target] + 1e-9).mean()


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
def predict(model, masked_plan):
    data = to_graph(masked_plan).to(DEVICE)
    return int(model(Batch.from_data_list([data])).argmax())


# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------
_model = None


def load_model():
    global _model
    if _model is None and WEIGHTS_PATH.exists():
        m = BottleneckGCN().to(DEVICE)
        m.load_state_dict(torch.load(WEIGHTS_PATH, map_location=DEVICE))
        m.eval()
        _model = m
    return _model


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


def find_bottleneck(masked_plan):
    """Bottleneck node of a masked EXPLAIN plan, with a readable reason and evidence.
    Uses the trained GCN if weights exist, otherwise the highest-self-cost baseline."""
    model = load_model()
    if model is None:
        return explain_node(masked_plan, baseline(masked_plan), "baseline (no trained weights)")
    return explain_node(masked_plan, predict(model, masked_plan), "gnn")


if __name__ == "__main__":
    from collections import defaultdict

    from ai import plan_gen

    plans = plan_gen.make_plans()
    tr, te = plan_gen.split(plans)
    print(f"device: {DEVICE} ({torch.cuda.get_device_name(0) if DEVICE.type == 'cuda' else 'cpu'})")
    t0 = time.time()
    model = train(tr, verbose=True)
    print(f"trained on {len(tr)} plans in {time.time() - t0:.1f} s")

    def score(ps):
        by_rule = defaultdict(lambda: [0, 0, 0])      # rule -> [gnn right, baseline right, n]
        for p in ps:
            r = by_rule[p["label_rule"]]
            r[0] += predict(model, p["plan"]) == p["label"]
            r[1] += baseline(p["plan"]) == p["label"]
            r[2] += 1
        return by_rule

    for title, ps in (("TRAIN shapes", tr), ("HELD-OUT shapes " + str(sorted(plan_gen.TEST_SHAPES)), te)):
        by = score(ps)
        g = sum(v[0] for v in by.values()); b = sum(v[1] for v in by.values()); n = sum(v[2] for v in by.values())
        print(f"\n{title}: GNN {100 * g / n:.1f}%  vs  baseline {100 * b / n:.1f}%   ({n} plans)")
        for rule, (gr, br, k) in sorted(by.items()):
            print(f"   {rule:18} GNN {gr:3}/{k:<3}  baseline {br:3}/{k}")

    torch.save(model.state_dict(), WEIGHTS_PATH)
    print(f"\nweights saved to {WEIGHTS_PATH.name}")

    print("\nEXAMPLE OUTPUTS (held-out plans):")
    for rule in ("highest_self_cost", "repeated_inner", "misestimate"):
        p = next(p for p in te if p["label_rule"] == rule)
        out = find_bottleneck(p["plan"])
        ok = "correct" if out["node_id"] == p["label"] else f"WRONG (label {p['label']})"
        print(f"\n[{p['id']}] label rule: {rule} -> GNN picked node {out['node_id']} ({ok}); "
              f"baseline picked node {baseline(p['plan'])}")
        print({k: out[k] for k in ("node_id", "operator", "relation", "rows", "cost_share_pct")})
        print("reason:", out["reason"])
