# ai/ results: evidence from REAL runs only

All numbers below come from the real Postgres database (1M rows in the largest table, seed 42) through the same `ai/`
code the pipeline uses. **No fake-world numbers are in this file.** Names in the tables are masked
exactly as `ai/` sees them; one clearly labelled section near the end un-masks them for the reader.

**How to re-run.** Each section names its command. `p3_evidence.py` un-masks queries (it plays the
pipeline's role), so it must live **outside** `ai/`. Today it is in Person 3's scratchpad together
with `p3_write.py`; moving both into the repo (outside `ai/`) is a lead decision. Each command
re-records its section in `evidence.json`; `python p3_evidence.py all` re-runs everything and
regenerates this file. Timings vary a little between runs; costs and same-results checks do not.

## 1. Rewrites: judged by real time + same results
Command: `python p3_evidence.py rewrites` (1 warm-up + median of 5 timed runs each, read-only connection;
same results = equal row count AND equal order-independent checksum of all rows).

| Query | Rule | Original ms | Rewritten ms | Speedup | Same results | Planner cost before -> after |
|---|---|---:|---:|---:|---|---|
| q04 | `correlated_subquery_to_join` | 2,204.0 | 216.8 | **10.2x** | yes | 1,369,978 -> 22,263 |
| q07 | `not_in_to_not_exists` | 362.1 | 161.8 | **2.2x** | yes | 23,329 -> 28,709 |
| q05 | `function_to_range` | 60.7 | 24.9 | **2.4x** | yes | 17,898 -> 16,904 |

Note q07: the planner **estimated the rewrite 23% more expensive**, but it measured **2.2x faster**. That is why rewrites are judged by real time.

## 2. Indexes: HypoPG cost (no real index is ever created)
Command: `python p3_evidence.py indexes` (`rl.recommend()` with the saved agent `ai/rl_agent.json`, real `cost_fn`
= Person 1's `sandbox.cost()` on a read-only connection with HypoPG, masked real table stats).
Top recommendation per query.

| Query | Split | Top fix (masked) | Cost before -> after | Drop | Write penalty | verified_by | Novelty |
|---|---|---|---|---:|---:|---|---|
| q11 | held-out | `CREATE INDEX ON t_1c1 (c_0b8, c_02b)` | 19,442 -> 9,430 | 51.5% | 0.06 | `hypopg_cost` | unseen shape (lower confidence) |
| q03 | held-out | `CREATE INDEX ON t_1c1 (c_073, c_02b)` | 16,923 -> 2,390 | 85.9% | 0.06 | `hypopg_cost` | unseen shape (lower confidence) |
| q01 | train | `CREATE INDEX ON t_1c1 (c_1e3)` | 15,908 -> 2,744 | 82.7% | 0.03 | `hypopg_cost` | seen query shape |
| q02 | train | `CREATE INDEX ON t_1c1 (c_02b)` | 18,745 -> 12,744 | 32.0% | 0.03 | `hypopg_cost` | seen query shape |
| q06 | held-out | `CREATE INDEX ON t_0a3 (c_edb)` | 16,051 -> 16,035 | 0.1% | 0.03 | `hypopg_cost` | unseen shape (lower confidence) |
| q09 | train | `CREATE INDEX ON t_1c1 (c_1e3); CREATE INDEX ON t_1c1 (c_c3a)` | 16,879 -> 1,133 | 93.3% | 0.06 | `hypopg_cost` | seen query shape |
| q10 | train | `CREATE INDEX ON t_1c1 (c_c3a)` | 16,873 -> 1,022 | 93.9% | 0.03 | `hypopg_cost` | seen query shape |
| q08 | train | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 18,582 -> 18,582 | 0.0% | 0.03 | `rule_only` | seen query shape |

q11 (the problem statement's example), `summary`: _Add composite index on t_1c1 (c_0b8, c_02b) (52% lower estimated cost in the HypoPG sandbox, +2 index columns to maintain on writes, write penalty 0.06). Zero raw data exposed to the AI (masked metadata only)._

q11, `why_not`: _Index on t_1c1(c_0b8) rejected: c_0b8 has only about 10 distinct values (10% of rows per value), so the planner would still read much of the table (simulated cost drop 16% vs 52% for the chosen fix)._

## 3. Bottleneck detection on the 11 real plans
Command: `python p3_evidence.py bottleneck` (`EXPLAIN (ANALYZE, FORMAT JSON)`, masked: allow-listed fields, hashed names).
Label = the step with the highest **measured** self time: actual time x loops minus children
(strict; none of the four methods sees these timings). Raw GNN / gated hybrid use the local
`ai/gnn_weights.pt` (synthetic training: `python -m ai.gnn`, seed 11; validated = False).

| Query | Measured label (masked) | baseline | rules | raw GNN | gated hybrid |
|---|---|---|---|---|---|
| q01 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (93%) | OK Seq Scan t_1c1 (93%) | OK Seq Scan t_1c1 (93%) | OK Seq Scan t_1c1 (93%) |
| q02 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (85%) | OK Seq Scan t_1c1 (85%) | MISS Seq Scan t_21e (6%) | OK Seq Scan t_1c1 (85%) |
| q03 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q04 | Seq Scan t_1c1 | MISS Index Scan t_21e (0%) | OK Seq Scan t_1c1 (100%) | MISS Index Scan t_21e (0%) | MISS Index Scan t_21e (0%) |
| q05 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q06 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) |
| q07 (2) | Seq Scan t_21e | MISS Seq Scan t_1c1 (100%) | MISS Seq Scan t_1c1 (100%) | MISS Seq Scan t_1c1 (100%) | MISS Seq Scan t_1c1 (100%) |
| q08 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (89%) | OK Seq Scan t_1c1 (89%) | OK Seq Scan t_1c1 (89%) | OK Seq Scan t_1c1 (89%) |
| q09 (1) | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q10 (1) | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q11 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (82%) | OK Seq Scan t_1c1 (82%) | OK Seq Scan t_1c1 (82%) | OK Seq Scan t_1c1 (82%) |

**Headline (strict measured labels):** baseline **9/11**, rules **10/11**, raw GNN **8/11**, gated hybrid **9/11**.

(1) The parallel formula (time x loops adds the workers together) decides the label here; counting
workers once would label the Gather step above it. The agreed formula was kept. Which queries get
this flag varies with timing noise (q06 was flagged in an earlier run).

(2) q07: Postgres books the time of building the hashed `NOT IN` list (from the subquery's scan) to
the parent scan, so the strict label is the parent scan and every method misses it. Labelling q07 as
the subquery's scan instead gives: baseline 10/11, rules 11/11, raw GNN 9/11, gated hybrid 10/11.

Earlier GNN test on 101 real plan variants with measured labels (scratch harness `real_label_gnn.py`):
split A (held out q03/q06/q11) all four methods 30/30; split B (held out q04/q07/q11) rules 20/21,
baseline 10/21, gated hybrid 10/21, raw GNN 0/21.

## 4. Generalization: queries the agent never trained on (real costs)
Command: `python p3_evidence.py generalization` (agent `ai/rl_agent.json`, trained on q01, q02, q04, q05, q07, q08,
q09, q10; held out q03, q06, q11). Partitions not offered (cannot be simulated yet), so the comparison
is like-for-like. Reward = cost drop minus 0.03 per index column.

| Method | q03 drop | q06 drop | q11 drop | Avg drop | Avg write penalty | Avg reward |
|---|---:|---:|---:|---:|---:|---:|
| RL alone | 39.1% | 0.0% | 15.9% | 18.3% | 0.030 | 0.153 |
| RL + top-3 verify | 85.9% | 0.1% | 51.5% | 45.8% | 0.050 | **0.408** |
| random | 50.4% | 5.4% | 14.4% | 23.4% | 0.040 | 0.194 |
| index-everything | 74.6% | 21.7% | 36.8% | 44.4% | 0.090 | 0.354 |
| best possible | 85.9% | 21.6% | 51.5% | 53.0% | 0.050 | 0.480 |

RL + top-3 verify costs 4 `cost_fn` calls per query (baseline + 3 fixes); best possible tries every candidate.

## 5. Limits: read before quoting any number
- **Partitioning is not simulated.** The `cost()` on master accepts `CREATE INDEX` only, so partition
  suggestions are `verified_by: rule_only` and carry no number (q08's likely fix is invisible).
- **Planner cost misjudges rewrites** (q07: estimated worse, measured faster). We handle it by judging
  rewrites only by real time on the original vs the rewritten query **plus** a same-results check
  (row count + checksum, or the pipeline's `results_match`); without that they stay `rule_only`.
- **The agent trained on only 8 real queries** (18 distinct simulations). Its blind pick on held-out
  queries is no better than random (0.153 vs 0.194); the top-3 verification is what makes it work.
- **The GNN does not beat the rules on real plans** (section 3), so the shipped default is `rules`.
  Real training data is very skewed (most bottlenecks are the same big scan).
- **The rules and the labelling ideas were written by the same team.** Treat the rules score as
  indicative, not as an independent benchmark. The strict labels come from measured time, which no
  method sees, but the dataset is our own 11 queries.
- **Index gains are planner-cost estimates** (HypoPG), not measured time. q06 has no single index
  that helps much (best possible 21.6%).
- Small synthetic dataset (1M rows in the largest table, one machine). Timings vary between runs.

## 6. Un-masked for the reader
**This is the only section with real names.** `ai/` itself never sees them; the pipeline translates.

| Masked | Real |
|---|---|
| `c_02b` | `order_date` |
| `c_03e` | `segment` |
| `c_073` | `status` |
| `c_0b8` | `region_id` |
| `c_1e3` | `customer_id` |
| `c_822` | `email` |
| `c_82a` | `name` |
| `c_c3a` | `product_id` |
| `c_cf3` | `amount` |
| `c_edb` | `category` |
| `t_0a3` | `products` |
| `t_1c1` | `orders` |
| `t_21e` | `customers` |

## 7. Leak check of the whole ai/ folder
Command: `python p3_write.py` (scans after writing this file). Searched: the 4 sales table names
and their distinctive column names, plus 3 column names that are everyday words (name, status,
category), reported separately. The words are not repeated below, so the list does not leak itself.

Files scanned: .gitignore, __init__.py, candidates.py, demo.py, demo_q11_recorded.json, fake_env.py, fixtures.py, gnn.py, plan_gen.py, README.md, RESULTS.md, rewrite.py, rl.py, rl_agent.json, sqlinfo.py, verify.py, why_not.py (not `gnn_weights.pt`, it is not in git).

- Sales-name hits in code, fixtures and `rl_agent.json`: **0**
- Sales-name hits outside the un-masked section: **0**

Every hit:

| File | Line | Word type | Where |
|---|---:|---|---|
| demo_q11_recorded.json | 65 | everyday word | everyday-word use |
| demo_q11_recorded.json | 114 | everyday word | everyday-word use |
| demo_q11_recorded.json | 126 | everyday word | everyday-word use |
| demo_q11_recorded.json | 127 | everyday word | everyday-word use |
| demo_q11_recorded.json | 152 | everyday word | everyday-word use |
| fake_env.py | 53 | everyday word | everyday-word use |
| fake_env.py | 60 | everyday word | everyday-word use |
| fake_env.py | 69 | everyday word | everyday-word use |
| fake_env.py | 84 | everyday word | everyday-word use |
| fake_env.py | 143 | everyday word | everyday-word use |
| fake_env.py | 149 | everyday word | everyday-word use |
| fake_env.py | 156 | everyday word | everyday-word use |
| fake_env.py | 229 | everyday word | everyday-word use |
| fake_env.py | 257 | everyday word | everyday-word use |
| gnn.py | 266 | everyday word | everyday-word use |
| plan_gen.py | 9 | everyday word | everyday-word use |
| plan_gen.py | 48 | everyday word | everyday-word use |
| plan_gen.py | 56 | everyday word | everyday-word use |
| plan_gen.py | 58 | everyday word | everyday-word use |
| plan_gen.py | 62 | everyday word | everyday-word use |
| plan_gen.py | 64 | everyday word | everyday-word use |
| plan_gen.py | 82 | everyday word | everyday-word use |
| plan_gen.py | 94 | everyday word | everyday-word use |
| plan_gen.py | 157 | everyday word | everyday-word use |
| plan_gen.py | 175 | everyday word | everyday-word use |
| plan_gen.py | 177 | everyday word | everyday-word use |
| plan_gen.py | 178 | everyday word | everyday-word use |
| plan_gen.py | 185 | everyday word | everyday-word use |
| plan_gen.py | 187 | everyday word | everyday-word use |
| plan_gen.py | 203 | everyday word | everyday-word use |
| plan_gen.py | 216 | everyday word | everyday-word use |
| README.md | 26 | everyday word | everyday-word use |
| README.md | 35 | everyday word | everyday-word use |
| README.md | 133 | everyday word | everyday-word use |
| RESULTS.md | 116 | sales name | un-masked section (allowed) |
| RESULTS.md | 117 | sales name | un-masked section (allowed) |
| RESULTS.md | 118 | everyday word | un-masked section (allowed) |
| RESULTS.md | 119 | sales name | un-masked section (allowed) |
| RESULTS.md | 120 | sales name | un-masked section (allowed) |
| RESULTS.md | 121 | sales name | un-masked section (allowed) |
| RESULTS.md | 122 | everyday word | un-masked section (allowed) |
| RESULTS.md | 123 | sales name | un-masked section (allowed) |
| RESULTS.md | 124 | sales name | un-masked section (allowed) |
| RESULTS.md | 125 | everyday word | un-masked section (allowed) |
| RESULTS.md | 126 | sales name | un-masked section (allowed) |
| RESULTS.md | 127 | sales name | un-masked section (allowed) |
| RESULTS.md | 128 | sales name | un-masked section (allowed) |
| rewrite.py | 42 | everyday word | everyday-word use |
| rewrite.py | 138 | everyday word | everyday-word use |
| rewrite.py | 144 | everyday word | everyday-word use |
| rewrite.py | 172 | everyday word | everyday-word use |
| rewrite.py | 180 | everyday word | everyday-word use |
| rl.py | 119 | everyday word | everyday-word use |
| rl.py | 123 | everyday word | everyday-word use |
| rl.py | 335 | everyday word | everyday-word use |
| rl.py | 518 | everyday word | everyday-word use |
| rl.py | 520 | everyday word | everyday-word use |
| rl.py | 523 | everyday word | everyday-word use |
| rl.py | 525 | everyday word | everyday-word use |
| sqlinfo.py | 4 | everyday word | everyday-word use |
| sqlinfo.py | 40 | everyday word | everyday-word use |
| sqlinfo.py | 44 | everyday word | everyday-word use |
| sqlinfo.py | 49 | everyday word | everyday-word use |
| sqlinfo.py | 134 | everyday word | everyday-word use |
| sqlinfo.py | 135 | everyday word | everyday-word use |
| sqlinfo.py | 137 | everyday word | everyday-word use |
| sqlinfo.py | 138 | everyday word | everyday-word use |
| sqlinfo.py | 140 | everyday word | everyday-word use |
| sqlinfo.py | 173 | everyday word | everyday-word use |
| sqlinfo.py | 179 | everyday word | everyday-word use |
