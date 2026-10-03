# ai/ results: evidence from REAL runs only

All numbers come from the real Postgres database (1M rows in the largest table, seed 42) through
the same `ai/` code the pipeline uses, with Person 1's sandbox (`cost()` incl. simulated monthly
partitions, `table_stats()`, `checked_run()`, `measure_write_latency()`). **No fake-world numbers.**
Names are masked as `ai/` sees them; one labelled section un-masks them for the reader.

**Re-run:** each section names its command. `p3_evidence.py` / `p3_write.py` / `workload_rl.py` un-mask
queries (they play the pipeline's role), so they live **outside** `ai/` (Person 3's scratchpad today;
moving them into the repo outside `ai/` is a lead decision). `python p3_evidence.py all` re-runs every
section and regenerates this file. **Partition costs and write latency vary between runs** (section 6);
they are reported as observed ranges, headlined at the conservative end.

## 0. Headline: measured time of the recommended fixes (requirement 6)
Command: `python p3_evidence.py timing`. Original vs the top recommended fix, Person 1 `checked_run()` on the **sandbox
copies only** (index built inside a rolled-back transaction; partition = the partitioned copy).
3 runs each (each run is itself a median of P1's repeats); latest run's median shown, its range in
brackets. `recommend()` now does the same check itself when the pipeline passes a `time_fn`.

| Query | Fix (masked) | Before ms | After ms | Speedup | **Measured drop** | Measured, all runs | Planner estimate | Trust ratio (planner / measured) | Same results |
|---|---|---:|---:|---:|---:|---|---:|---:|---|
| q11 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 38.7 (34.5-45.1) | 16.9 (15.7-26.3) | 2.3x | **56.3%** | 58.9%, 56.3% | 91.2% | **1.62** (over) | yes |
| q03 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 95.6 (70.1-107.7) | 1.7 (1.5-2.3) | 54.9x | **98.2%** | 95.1%, 98.2% | 96.0% | **0.98** | yes |
| q01 | `CREATE INDEX ON t_1c1 (c_1e3)` | 34.4 (33.6-39.7) | 0.7 (0.3-0.8) | 52.9x | **98.1%** | 96.7%, 98.1% | 82.7% | **0.84** (under) | yes |
| q02 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 55.0 (50.6-57.3) | 13.8 (13.7-16.3) | 4.0x | **74.9%** | 61.8%, 74.9% | 91.2% | **1.22** (over) | yes |
| q06 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 44.6 (43.5-44.9) | 7.0 (6.9-7.2) | 6.4x | **84.3%** | 55.7%, 84.3% | 83.8% | **0.99** | yes |
| q08 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 56.9 (52.1-58.2) | 28.1 (26.7-30.4) | 2.0x | **50.7%** | 48.5%, 50.7% | 47.4% | **0.93** | yes |

**6 of 6 winners are more than 50% faster in measured time in the latest run** (5 of 6 in every run), all with identical results.

**Trust score:** the planner overestimates partition gains by about **1.15x to 1.31x on average** in our runs (planner % / measured %, averaged over the 5 partition winners). For the one index winner it *under*estimates (0.86x, 0.84x). **This is six samples per run, 2 runs** - an
indication, not a calibration. Biggest gaps: q11 and q02 (planner about 91-92%, measured 56-75%).
Treat planner numbers (sections 2 and 4) as a ranking signal; measured numbers are the real effect.

## 1. Rewrites: real time + same results (Person 1 `checked_run()`)
Command: `python p3_evidence.py rewrites`. Same results = equal row count AND equal checksum of all rows.

| Query | Rule | Original ms | Rewritten ms | Speedup | Same results | Planner cost before -> after |
|---|---|---:|---:|---:|---|---|
| q04 | `correlated_subquery_to_join` | 2,905.7 | 641.5 | **4.5x** | yes | 1,369,978 -> 22,263 |
| q07 | `not_in_to_not_exists` | 359.6 | 174.3 | **2.1x** | yes | 23,329 -> 28,709 |
| q05 | `function_to_range` | 53.8 | 17.8 | **3.0x** | yes | 17,898 -> 16,904 |

q07: the planner **estimated the rewrite 23% more expensive**, but it measured **2.1x faster**, so rewrites are judged by real time.

## 2. Indexes and partitions (top fix per query, as `recommend()` reports it)
Command: `python p3_evidence.py indexes`. `rl.recommend()` with `ai/rl_agent.json`, real `cost_fn` (HypoPG for indexes,
partitioned sandbox copy for `PARTITION ... MONTHLY`), masked `table_stats()`, and `write_fn` =
Person 1 `measure_write_latency()` for the final index fix. Drop / write cost = observed range (section 6).

| Query | Split | Top fix (masked) | Planner cost drop (range) | Measured gain (this run) | Measured write cost (range, insert time) | verified_by | Novelty |
|---|---|---|---:|---:|---:|---|---|
| q11 | held-out | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 66.8% to 92.2% | 66.2% | not measured (partition) | `timed_run` | unseen shape (lower confidence) |
| q03 | held-out | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 96.0% | 95.8% | not measured (partition) | `timed_run` | seen query shape |
| q01 | train | `CREATE INDEX ON t_1c1 (c_1e3)` | 82.7% | 98.8% | +129% to +215% | `timed_run` | seen query shape |
| q02 | train | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 73.2% to 92.0% | 65.3% | not measured (partition) | `timed_run` | unseen shape (lower confidence) |
| q06 | held-out | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 83.8% | 78.7% | not measured (partition) | `timed_run` | seen query shape |
| q09 | train | `CREATE INDEX ON t_1c1 (c_1e3); CREATE INDEX ON t_1c1 (c_c3a)` | 93.3% | 99.6% | +54% to +340% | `timed_run` | seen query shape |
| q10 | train | `CREATE INDEX ON t_1c1 (c_c3a)` | 93.9% | 99.2% | +2% to +203% | `timed_run` | unseen shape (lower confidence) |
| q08 | train | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 47.4% | 60.9% | not measured (partition) | `timed_run` | seen query shape |

q11 (the problem statement's example), `summary` (latest run): _Partition t_1c1 by c_02b (66% faster in measured sandbox time (planner estimated 91%); write cost not measured). Zero raw data exposed to the AI (masked metadata only)._

q11, `why_not`: _Index on t_1c1(c_0b8, c_02b) not chosen: it cuts the simulated cost by only 51% vs 91% for the chosen fix._

q01, `summary` with a measured write cost (latest run): _Add index on t_1c1 (c_1e3) (99% faster in measured sandbox time (planner estimated 83%), +0.00265 ms per inserted row, measured on the sandbox copy (+180% insert time)). Zero raw data exposed to the AI (masked metadata only)._

## 3. Bottleneck detection on the 11 real plans
Command: `python p3_evidence.py bottleneck`. `EXPLAIN (ANALYZE, FORMAT JSON)`, masked. Label = step with the highest
**measured** self time (actual time x loops minus children); none of the methods sees these timings.
Raw GNN / gated hybrid use the local `ai/gnn_weights.pt` (validated = False).

| Query | Measured label (masked) | baseline | rules | raw GNN | gated hybrid |
|---|---|---|---|---|---|
| q01 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (93%) | OK Seq Scan t_1c1 (93%) | OK Seq Scan t_1c1 (93%) | OK Seq Scan t_1c1 (93%) |
| q02 | Seq Scan t_21e | MISS Seq Scan t_1c1 (85%) | MISS Seq Scan t_1c1 (85%) | OK Seq Scan t_21e (6%) | MISS Seq Scan t_1c1 (85%) |
| q03 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q04 | Seq Scan t_1c1 | MISS Index Scan t_21e (0%) | OK Seq Scan t_1c1 (100%) | MISS Index Scan t_21e (0%) | MISS Index Scan t_21e (0%) |
| q05 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q06 (1) | Seq Scan t_1c1 | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) |
| q07 (2) | Seq Scan t_21e | MISS Seq Scan t_1c1 (100%) | MISS Seq Scan t_1c1 (100%) | MISS Seq Scan t_1c1 (100%) | MISS Seq Scan t_1c1 (100%) |
| q08 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (89%) | OK Seq Scan t_1c1 (89%) | OK Seq Scan t_1c1 (89%) | OK Seq Scan t_1c1 (89%) |
| q09 (1) | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q10 (1) | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q11 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (82%) | OK Seq Scan t_1c1 (82%) | OK Seq Scan t_1c1 (82%) | OK Seq Scan t_1c1 (82%) |

**Headline (strict measured labels, this run):** baseline **8/11**, rules **9/11**, raw GNN **9/11**, gated hybrid **8/11**.

(1) Parallel plan: time x loops adds the workers together and decides the label; counting workers once
would pick the step above. The agreed formula was kept. Flags and some labels change between runs
(e.g. q02 and q11's measured label moved between runs today), so treat this table as noisy.

(2) q07: Postgres books the hashed `NOT IN` list's time to the parent scan, so the strict label is the
parent and every method misses it. With q07 labelled as the subquery's scan: baseline 9/11, rules 10/11, raw GNN 10/11, gated hybrid 9/11.

## 4. Generalization: queries the agent never trained on
### 4a. Person 1's workload: 4 held-out templates, 100 queries
Command: `python workload_rl.py` (train + evaluate) or `--eval-only` (evaluate the saved agent).
Agent trained on 100 workload queries (templates t01-t08); tested on templates t09-t12 (25 queries each),
all masked. Reward = cost drop minus 0.03 per index column; a partition the sandbox refuses = no gain.

| Template | RL alone | RL + top-3 verify | random | index-everything | best possible | cost_fn calls (verify / best) |
|---|---:|---:|---:|---:|---:|---:|
| t09 | 0.930 (96%) | **0.930 (96%)** | 0.930 (96%) | 0.930 (96%) | 0.930 (96%) | 2 / 2.0 |
| t10 | 0.831 (87%) | **0.887 (94%)** | 0.525 (57%) | 0.577 (67%) | 0.910 (98%) | 4 / 9.0 |
| t11 | 0.859 (89%) | **0.888 (92%)** | 0.623 (66%) | 0.540 (60%) | 0.907 (95%) | 4 / 5.0 |
| t12 | 0.816 (85%) | **0.816 (85%)** | 0.131 (17%) | 0.206 (33%) | 0.816 (85%) | 4 / 8.0 |
| ALL | 0.859 (89%) | **0.880 (92%)** | 0.552 (59%) | 0.563 (64%) | 0.891 (94%) | 4 / 6.0 |

Novelty on the 100 held-out queries: **50 unseen shape**, 50 seen shape (t09 and t11 match shapes met in training).
t09 (`col IN ($1, $2, $3)`) had **no candidates** until a parser coverage fix (IN-list = exact-match
filter); before the fix only 75 queries could be scored. No training query has an IN-list, so the
agent was not retrained. t09 has one sensible candidate, so every method scores the same there.
t12 moved between runs (best possible 0.817 -> 0.631 with the SAME agent) because its winning partition
cost changes when the sandbox's partitioned copy is rebuilt (section 6).

### 4b. The 3 held-out slow queries (q03, q06, q11)
Command: `python p3_evidence.py generalization`.

| Method | q03 drop | q06 drop | q11 drop | Avg reward |
|---|---:|---:|---:|---:|
| RL alone | 96.0% | 83.8% | 92.1% | 0.876 |
| RL + top-3 verify | 96.0% | 83.8% | 92.1% | 0.876 |
| random | 61.8% | 21.1% | 24.1% | 0.318 |
| index-everything | 74.6% | 21.7% | 36.8% | 0.354 |
| best possible | 96.0% | 83.8% | 92.1% | 0.876 |

## 5. Before / after Person 1's sandbox
Before = `RESULTS.md` at commit `eac48d7` (indexes only, no partitions, agent trained on 8 queries).

| Item | Before | After |
|---|---|---|
| q11 top fix | index, 51.5% (`hypopg_cost`) | partition, 66.8% to 92.2% (`timed_run`) |
| q03 top fix | index, 85.9% (`hypopg_cost`) | partition, 96.0% (`timed_run`) |
| q02 top fix | index, 32.0% (`hypopg_cost`) | partition, 73.2% to 92.0% (`timed_run`) |
| q06 top fix | index, 0.1% (`hypopg_cost`) | partition, 83.8% (`timed_run`) |
| q08 top fix | partition, 0.0% (`rule_only`) | partition, 47.4% (`timed_run`) |
| q04 rewrite speedup | 10.2x (own timer) | 4.5x (`checked_run`) |
| q07 rewrite speedup | 2.2x (own timer) | 2.1x (`checked_run`) |
| q05 rewrite speedup | 2.4x (own timer) | 3.0x (`checked_run`) |
| q03/q06/q11 avg reward, RL + top-3 verify | 0.408 | 0.876 |
| q03/q06/q11 avg reward, RL alone | 0.153 | 0.876 |
| Agent training data | 8 real queries, 18 simulations | 100 workload queries, 277 simulations |
| Unseen-template test | none | 100 queries, 4 templates (section 4a) |
| Write cost in summary | width estimate ("estimated") | measured per inserted row (indexes) |
| Stats | read by Person 3's harness | Person 1 `table_stats()` |

## 6. Run-to-run variance (why some numbers are ranges)
Observed today (earlier runs + every fresh run of section 2):

| Number | Observed values | Conservative headline |
|---|---|---|
| q11 partition cost drop | 73.9%, 73.9%, 73.9%, 92.1%, 91.2%, 91.2%, 92.2%, 84.6%, 92.2%, 66.8%, 91.2% | **66.8%** |
| q02 partition cost drop | 73.2%, 73.2%, 73.2%, 91.9%, 91.2%, 91.2%, 92.0%, 92.0%, 92.0% | **73.2%** |
| q10 index write cost (insert time) | +203%, +97%, +53%, +123%, +146%, +94%, +58%, +73%, +120%, +2%, +66% | **+203%** |
| q01 index write cost (insert time) | +161%, +156%, +215%, +129%, +145%, +180%, +210%, +180%, +129% | **+215%** |
| q09 index write cost (insert time) | +340%, +188%, +177%, +105%, +54%, +127%, +212%, +231% | **+340%** |

Why: each time the sandbox's partitioned copy is rebuilt (teammates / `generate.py` drop stale copies),
Person 1's code runs `ANALYZE`, which **samples rows at random**, so the planner's statistics and cost
estimates change slightly; q11 moves between about -67% and -92%. Within one build the cost is stable.
Write latency is a real timing of small insert batches on a shared machine, so it is noisy too.
Index costs (HypoPG) did not vary.

## 7. Limits: read before quoting any number
- **Planner estimates vs measured time:** sections 2 and 4 use planner costs (HypoPG / partitioned
  copy). Section 0 measures the winners: partition gains are overestimated by the planner (q11, q02,
  q06), index gains were underestimated (q01). Quote section 0 for real speedups.
- **Run-to-run variance** in partition costs and write latency (section 6); quote the conservative end.
- **MONTHLY only (contract):** partitions are proposed and simulated as `PARTITION ... MONTHLY` only.
- `range_sel` comes from Person 1's `table_stats()`; the contract fallback (estimate from the masked
  plan) exists in `ai/rl.py` but never fired here: P1 covered 6/6 range columns of the slow queries
  and 138/138 in the workload.
- **The GNN does not beat the rules on real plans** (section 3: tie or loss in every test, 0/22 vs
  22/22 on repeated-scan plans in a 301-plan test); the shipped default is `rules`.
- Rewrite speedups also vary between runs (e.g. q04 4.5x to 10.2x today); same results every time.
- **The rules and the labelling ideas were written by the same team**: treat the rules score as
  indicative, not an independent benchmark; the dataset is our own 11 queries + generated workload.
- Planner cost misjudges rewrites (section 1): rewrites are accepted only with real timing + same results.
- One evidence run failed once while a teammate used the shared sandbox at the same time (lock / timeout).
- Small synthetic dataset (1M rows in the largest table, one machine).

## 8. Un-masked for the reader
**This is the only section with real names.** `ai/` itself never sees them; the pipeline translates.

| Masked | Real |
|---|---|
| `c_02b` | `order_date` |
| `c_073` | `status` |
| `c_0b8` | `region_id` |
| `c_1e3` | `customer_id` |
| `c_c3a` | `product_id` |
| `t_1c1` | `orders` |
| `t_21e` | `customers` |

## 9. Leak check of the whole ai/ folder
Command: `python p3_write.py`. Searched: the 4 sales table names and their distinctive column names,
plus 3 column names that are everyday words (name, status, category), reported separately. Words are
not repeated below, so the list does not leak itself.

Files scanned: .gitignore, __init__.py, candidates.py, demo.py, demo_q11_recorded.json, fake_env.py, fixtures.py, gnn.py, plan_gen.py, README.md, RESULTS.md, rewrite.py, rl.py, rl_agent.json, sqlinfo.py, test_measured.py, test_range_sel.py, verify.py, why_not.py (not `gnn_weights.pt`, it is not in git).

- Sales-name hits in code, fixtures, `rl_agent.json` and demo files: **0**
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
| README.md | 150 | everyday word | everyday-word use |
| RESULTS.md | 188 | sales name | un-masked section (allowed) |
| RESULTS.md | 189 | everyday word | un-masked section (allowed) |
| RESULTS.md | 190 | sales name | un-masked section (allowed) |
| RESULTS.md | 191 | sales name | un-masked section (allowed) |
| RESULTS.md | 192 | sales name | un-masked section (allowed) |
| RESULTS.md | 193 | sales name | un-masked section (allowed) |
| RESULTS.md | 194 | sales name | un-masked section (allowed) |
| rewrite.py | 42 | everyday word | everyday-word use |
| rewrite.py | 138 | everyday word | everyday-word use |
| rewrite.py | 144 | everyday word | everyday-word use |
| rewrite.py | 172 | everyday word | everyday-word use |
| rewrite.py | 180 | everyday word | everyday-word use |
| rl.py | 123 | everyday word | everyday-word use |
| rl.py | 127 | everyday word | everyday-word use |
| rl.py | 344 | everyday word | everyday-word use |
| rl.py | 380 | everyday word | everyday-word use |
| rl.py | 654 | everyday word | everyday-word use |
| rl.py | 656 | everyday word | everyday-word use |
| rl.py | 659 | everyday word | everyday-word use |
| rl.py | 661 | everyday word | everyday-word use |
| sqlinfo.py | 4 | everyday word | everyday-word use |
| sqlinfo.py | 40 | everyday word | everyday-word use |
| sqlinfo.py | 44 | everyday word | everyday-word use |
| sqlinfo.py | 49 | everyday word | everyday-word use |
| sqlinfo.py | 139 | everyday word | everyday-word use |
| sqlinfo.py | 140 | everyday word | everyday-word use |
| sqlinfo.py | 142 | everyday word | everyday-word use |
| sqlinfo.py | 143 | everyday word | everyday-word use |
| sqlinfo.py | 145 | everyday word | everyday-word use |
| sqlinfo.py | 178 | everyday word | everyday-word use |
| sqlinfo.py | 184 | everyday word | everyday-word use |
| test_measured.py | 10 | everyday word | everyday-word use |
| test_range_sel.py | 9 | everyday word | everyday-word use |
