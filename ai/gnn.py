"""GNN over the (masked) plan tree that flags the bottleneck node.

Owner: Person 3.  STATUS: STUB.  Sees ONLY masked data.
Self-test:  .venv/Scripts/python -m ai.gnn
"""


def find_bottleneck(masked_plan):
    root = masked_plan["Plan"]
    return {"node_id": 0, "op": root["Node Type"],
            "reason": "Full scan of the largest table dominates the plan cost.",
            "evidence": {"table": root.get("Relation Name"), "cost_share": 0.92}}


if __name__ == "__main__":
    print(find_bottleneck({"Plan": {"Node Type": "Seq Scan", "Relation Name": "t_a3f"}}))
