# ai/ — RL + GNN + SQL rewrites (Person 3)

Everything in this folder sees **masked data only**: table names `t_xxx`, columns
`c_xxx`, output names `x_xxx`, values `$1, $2…` (or `?`). The real database is
reached only through functions the pipeline hands in (`cost_fn`, `time_fn`).

## Function contract

```python
from ai import gnn, rl, rewrite

gnn.find_bottleneck(masked_plan, path_setting=None) -> dict
rl.recommend(masked_sql, masked_plan, cost_fn,
             stats=None, time_fn=None, agent=None, column_types=None) -> list[dict]
rewrite.rewrite_sql(masked_sql) -> list[{"sql", "rule"}]
```

### `gnn.find_bottleneck(masked_plan)`
Input: an `EXPLAIN (FORMAT JSON)` plan, masked, as `{"Plan": {...}}`. Plans run with
`ANALYZE` may include `"Actual Rows"` / `"Actual Loops"`; they are used when present.

| key | meaning |
|---|---|
| `node_id` | pre-order index of the bottleneck step |
| `operator`, `op` | e.g. `Seq Scan` (`op` = contract key) |
| `relation` | masked table name, or `None` |
| `rows` | planner's row estimate for that step |
| `cost_share_pct` | its share of the plan's cost, counting repeats (loops) |
| `reason` | one readable sentence built from the numbers (no LLM) |
| `evidence` | the numbers behind the reason (contract key) |
| `path` | which method picked it: `rules` / `gated_hybrid` / `gnn` |

**Three paths** (setting `BOTTLENECK_PATH` at the top of `gnn.py`; default **`rules`**):

| path | how it picks | status |
|---|---|---|
| `rules` | highest self cost, plus two plain if-checks: a badly misestimated scan (actual ≥ 50× estimate), and a Seq Scan that really runs many times (Nested Loop inner / correlated SubPlan) | **default** — best on the 11 real plans |
| `gated_hybrid` | the baseline, but the GNN may pick a rule-flagged node if it is ≥ 80% sure | needs `gnn_weights.pt` |
| `gnn` | the trained 2-layer GCN alone (baseline score + learned correction) | needs weights; **not validated** (held-out normal plans 54/59 vs baseline 59/59) |
| `auto` | `gnn` if the weights file says `validated=True`, else `gated_hybrid` | |

**The default path (`rules`) does not need any weights.** `ai/gnn_weights.pt` is
not in git (see `ai/.gitignore`); it is only needed for `gated_hybrid`, `gnn` or `auto`.
Without a weights file every path falls back to `rules`.

Retrain the weights (from the project root, ~25 s on the RTX 5060, CPU also works):

```
.venv\Scripts\python -m ai.gnn
```

This trains on synthetic masked plans from `ai/plan_gen.py`, prints the held-out
table, and writes `ai/gnn_weights.pt` with its `validated` flag (currently `False`:
54/59 vs baseline 59/59 on held-out normal plans; on real plans with measured labels
it lost to `rules` on both splits tried).

### `rl.recommend(masked_sql, masked_plan, cost_fn, ...)`
Returns recommendations, best first. Contract keys: `kind`, `action`, `cost_before`,
`cost_after`, `reason`. Added keys (additions only):

| key | meaning |
|---|---|
| `improvement_pct` | cost drop (index/partition) or time drop (rewrite, if timed) |
| `reward` | improvement minus write penalty (what the agent maximises) |
| `write_penalty` | 0.03 per index column; rewrites 0 |
| `verified_by` | `hypopg_cost` = simulated with HypoPG; `timed_run` = really run, same results checked; `rule_only` = NOT simulated (e.g. partitioning today) |
| `novelty` | `seen query shape` or `unseen shape (lower confidence)` vs the agent's training |
| `agent_score` | the agent's predicted reward before simulation |
| rewrites only | `same_results`, `time_before_ms`, `time_after_ms`, `speedup`, `verify_note`, `rule_label` |
| first item only | `why_not` (why the best alternative was rejected), `summary` (whole bundle in one sentence; numbers only for verified parts) |

`kind` is `index`, `partition`, `rewrite`, or `none` (masked SQL could not be parsed).
How it works: the agent ranks all candidates (`ai/candidates.py`), then the top 3 are
really simulated with `cost_fn` (4 calls per query) and sorted by measured reward.
Fixes measured as no better are dropped; unsimulated ones stay, flagged `rule_only`.

**Trained agent** — `ai/rl_agent.json` (structural features only, no names): trained on
**real planner costs** (Person 1 `sandbox.cost()`, HypoPG + simulated `PARTITION ... MONTHLY`)
for the **100 training queries of Person 1's workload generator** (`db/workload.py`,
`generate(split="train")`, seed 7: templates t01-t08, 12-13 queries each), all masked, stats
from `sandbox.table_stats()` (masked). 3000 episodes, seed 7, 277 distinct simulations.
A partition the sandbox refuses (non-date column) counted as no gain during training.
The 100 test queries (templates t09-t12) were held out. 7 query shapes were seen in
training; held-out t09/t10/t12 correctly show `unseen shape`, t11 matches a seen shape.
Missing file → an untrained agent (everything `unseen`; the top-3 simulation still picks).

## What Person 1 / 2 / 4 must pass in

**Person 4 (pipeline)** builds these and passes them to `recommend()`:

```python
cost_fn(masked_sql, masked_actions) -> float
#   un-mask both privately, then  db.sandbox.cost(sql, index_sqls)
#   - masked_actions are "CREATE INDEX ON t_x (c_y, ...)" strings
#   - for a rewrite, masked_sql IS the rewritten query and masked_actions == []
#   - raise ValueError for actions the sandbox can't simulate (e.g. "PARTITION ...");
#     recommend() then marks them rule_only instead of crashing

time_fn(masked_sql) -> dict                                            # optional
#   Called twice per rewrite: first the ORIGINAL, then the REWRITE. Two shapes accepted:
#   A) {"ms": float, "results_match": bool}   <- P4's choice: rows/checksum stay in the
#      pipeline; "results_match" on the 2nd call = rewrite returned the same rows as the 1st.
#   B) {"ms": float, "rows": int, "checksum": str}   <- contract v2 (P1 checked_run);
#      ai/ compares count + checksum itself.
#   Call styles: if time_fn has a `compare_to` parameter, ai/ calls
#   time_fn(rewritten_sql, compare_to=original_sql) (preferred: no hidden state);
#   otherwise time_fn(rewritten_sql) right after time_fn(original_sql).
#   Without it, rewrites are rule_only (planner cost is unreliable for rewrites).

stats = {                                                              # optional, recommended
  "rows":      {"t_x": 1000000.0, ...},            # table sizes (pg_class.reltuples)
  "eq_sel":    {"t_x.c_y": 1/n_distinct, ...},     # P4 fills this in; OR send
  "n_distinct": {"t_x.c_y": n, ...},               #   n_distinct (contract v2) -> eq_sel = 1/n
  "range_sel": {"t_x.c_y": 0.03, ...},             # planner row estimate for the query's range / table rows
  "existing_indexes": [("t_x", ("c_y",)), ...],    # ALL existing indexes incl. primary keys
}
#   Without stats, rough sizes are read from the plan (after filters, so too small).
```

### Trust boundary
`results_match` is computed on the pipeline/sandbox side, where the real rows are.
**ai/ cannot check it and simply trusts it.** If the pipeline reported
`results_match: True` for a rewrite that changes the results, ai/ would accept that
rewrite. The guarantee "same results" is therefore only as good as the pipeline's
comparison (row count + order-independent checksum of all rows, per contract v2).
The same applies to `cost_fn` and `stats`: ai/ trusts the numbers it is given.

**Person 1 (sandbox):** `cost()` exists. Still needed: a run that returns rows +
checksum for `time_fn` (today `timed_run()` returns ms only), and the masked
`existing_indexes` + table stats above. Partitioning simulation would turn
`rule_only` partition suggestions into verified ones.

**Person 2 (privacy):**
- `mask_query()` must return **valid SQL** (the stub's `SELECT ...` cannot be parsed).
- Number the placeholders `$1, $2…` (the date-range rewrite reuses a value).
- Keep date-unit words like `'MONTH'` (SQL keywords, not data).
- Optional: column type category (`date` / `number` / `text`) → `column_types`.
- `unmask()` must also translate `reason`, `why_not` and `summary` text (they contain masked names).

## Files

| file | what |
|---|---|
| `sqlinfo.py` | reads masked SQL: column roles, join pairs |
| `rewrite.py` | 3 rewrite rules (correlated subquery → JOIN, NOT IN → NOT EXISTS, function → range) |
| `candidates.py` | all fixes worth trying (indexes, OR pairs, partitions, rewrites), minus existing indexes |
| `rl.py` | contextual-bandit agent, top-3 verify, `recommend()`, summary |
| `verify.py` | rewrite check by real time + same results |
| `why_not.py` | evidence sentence for the rejected alternative |
| `gnn.py` | plan → graph, GCN, rules, three paths, `find_bottleneck()` |
| `plan_gen.py`, `fake_env.py`, `fixtures.py` | synthetic masked training/test data |
| `rl_agent.json` | trained agent (structural features only) |

Self-tests: `.venv/Scripts/python -m ai.<module>` for every file above.
