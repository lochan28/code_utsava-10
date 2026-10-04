"""Shared setup for the tests that run the WHOLE pipeline with the real masker and sandbox.

- the real masked names (they come from the team's secret key, so tests look them up);
- stand-ins for the AI pieces (ai/ changes as Person 3 delivers) and fast fakes for the slow
  real measurements, so these tests check OUR code and not which ai/ happens to be merged.
Not a test file itself.
"""
from ai import gnn, rl
from db import sandbox
from privacy import mask

MAPPING = mask.build_mapping()
T = MAPPING.tables["orders"]                               # masked name of the table "orders"
C_REGION = MAPPING.columns["region_id"]
C_CUSTOMER = MAPPING.columns["customer_id"]


def stand_in_recommend(masked_sql, masked_plan, cost_fn):
    """One plain index recommendation, in the old 3-argument shape, with no verified_by."""
    return [{"kind": "index", "action": f"CREATE INDEX ON {T} ({C_REGION})", "cost_before": 100.0,
             "cost_after": 10.0, "reason": "No index on the filter column."}]


def stand_in_bottleneck(masked_plan):
    return {"node_id": 0, "op": "Seq Scan", "reason": "Full scan of the largest table.",
            "evidence": {"table": T, "cost_share": 0.92}}


FAST_MEASUREMENTS = [(sandbox, "timed_run", lambda sql, idx=[]: 4.0 if idx else 10.0),
                     (sandbox, "measure_write_latency", lambda index_sql: {"delta_ms": 0.001}),
                     (sandbox, "index_size_bytes", lambda index_sql: 1_000_000)]

# (object, name, replacement) triples: enter each with the test's own `patched(...)`.
AI_STAND_INS = [(rl, "recommend", stand_in_recommend), (gnn, "find_bottleneck", stand_in_bottleneck),
                *FAST_MEASUREMENTS]
