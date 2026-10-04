"""Simulation accuracy report: how close are our simulated numbers to reality?

Owner: Person 1.
For each recommended index it compares
  - storage: HypoPG's instant estimate vs the real size after building it,
  - write latency: our sandbox measurement repeated several times, to show
    how repeatable it is. (Write latency is MEASURED with the real index on a
    sandbox copy, not estimated, so there is no estimate to compare against.)

Run:  .venv/Scripts/python -m db.accuracy
"""
import statistics

from db import sandbox

# ============================================================
# TUNABLE SETTINGS
# ============================================================
WRITE_RUNS = 3   # how many times the write-latency measurement is repeated per
                 # index; more = better picture of the spread, ~1 s each.

INDEXES = {      # the recommended index for each demo query
    "q01": "CREATE INDEX ON orders (customer_id)",
    "q09": "CREATE INDEX ON orders (product_id)",
    "q10": "CREATE INDEX ON orders (product_id)",
    "q11": "CREATE INDEX ON orders (region_id, order_date)",
}


def report():
    """Rows of {"query", "index", "size_est_mb", "size_real_mb", "size_error_pct",
    "write_us_runs", "write_us_median", "write_spread_us"}."""
    rows = []
    for qid, idx in INDEXES.items():
        est, real = sandbox.hypopg_index_size_bytes(idx), sandbox.index_size_bytes(idx)
        writes = [sandbox.measure_write_latency(idx)["delta_ms"] * 1000 for _ in range(WRITE_RUNS)]
        rows.append({"query": qid, "index": idx[16:],
                     "size_est_mb": est / 1e6, "size_real_mb": real / 1e6,
                     "size_error_pct": 100 * (est - real) / real,
                     "write_us_runs": writes, "write_us_median": statistics.median(writes),
                     "write_spread_us": max(writes) - min(writes)})
    return rows


if __name__ == "__main__":
    print(f"{'query':5} {'index':32} {'size est':>9} {'real':>8} {'error':>7}   "
          f"{'extra write per row (us)':>26}")
    for r in report():
        runs = ", ".join(f"{w:.1f}" for w in r["write_us_runs"])
        print(f"{r['query']:5} {r['index']:32} {r['size_est_mb']:7.1f}MB {r['size_real_mb']:6.1f}MB "
              f"{r['size_error_pct']:+6.0f}%   median {r['write_us_median']:.1f} (runs {runs}; spread {r['write_spread_us']:.1f})")
