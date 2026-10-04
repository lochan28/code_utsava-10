# ai/ results: evidence from REAL runs only

All numbers come from the real Postgres database (5M rows in the largest table) through
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
5 runs each (each run is itself a median of P1's repeats); latest run's median shown, its range in
brackets. `recommend()` now does the same check itself when the pipeline passes a `time_fn`.

| Query | Fix (masked) | Before ms | After ms | Speedup | **Measured drop** | Measured, all runs | Planner estimate | Trust ratio (planner / measured) | Same results |
|---|---|---:|---:|---:|---:|---|---:|---:|---|
| q11 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY; CREATE INDEX ON t_1c1 (c_0b8)` | 137.1 (122.7-141.7) | 54.2 (35.8-69.8) | 2.5x | **60.4%** | 75.3%, 60.4% | 96.3% | **1.59** (over) | yes |
| q03 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY; CREATE INDEX ON t_1c1 (c_073, c_02b)` | 104.2 (99.7-106.6) | 2.4 (1.9-2.6) | 43.3x | **97.7%** | 97.8%, 97.7% | 98.2% | **1.01** | yes |
| q01 | `CREATE INDEX ON t_1c1 (c_1e3)` | 85.0 (77.2-94.4) | 5.4 (4.3-7.9) | 15.8x | **93.7%** | 95.2%, 93.7% | 85.2% | **0.91** | yes |
| q02 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 138.6 (117.0-139.8) | 43.8 (40.0-50.3) | 3.2x | **68.4%** | 68.5%, 68.4% | 94.4% | **1.38** (over) | yes |
| q06 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 89.6 (88.9-102.5) | 27.5 (26.5-28.4) | 3.3x | **69.3%** | 71.0%, 69.3% | 87.3% | **1.26** (over) | yes |
| q08 | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 190.5 (187.9-210.2) | 132.5 (128.2-134.7) | 1.4x | **30.4%** | 27.8%, 30.4% | 50.2% | **1.65** (over) | yes |

**5 of 6 winners are more than 50% faster in measured time in the latest run** (5 of 6 in every run), all with identical results.

**Trust score:** the planner overestimates partition gains by about **1.34x to 1.38x on average** in our runs (planner % / measured %, averaged over the 5 partition winners, incl. partition + index). Index winners: 0.89x, 0.91x. **This is 6 samples per run, 2 runs** - an
indication, not a calibration. Biggest gaps: q11 (planner 96%, measured 60%), q02 (planner 94%, measured 68%).
Treat planner numbers (sections 2 and 4) as a ranking signal; measured numbers are the real effect.

## 1. Rewrites: real time + same results (Person 1 `checked_run()`)
Command: `python p3_evidence.py rewrites`. Same results = equal row count AND equal checksum of all rows.

| Query | Rule | Original ms | Rewritten ms | Speedup | Same results | Planner cost before -> after |
|---|---|---:|---:|---:|---|---|
| q04 | `correlated_subquery_to_join` | 13,613.5 | 852.5 | **16.0x** | yes | 6,618,245 -> 85,510 |
| q07 | `not_in_to_not_exists` | > 30,000 (sandbox timeout) | 656.3 | **>45.7x** | not checkable (original timed out) | 3,871,068,233 -> 136,641 |
| q05 | `function_to_range` | 245.2 | 78.2 | **3.1x** | yes | 85,309 -> 80,356 |

q07: on 5M rows the **original no longer finishes within the sandbox's 30 s limit**; the rewrite runs in 656 ms (**more than 46x faster**). Same results cannot be checked here (the original never completes), so this rewrite stays unverified. The planner estimated it -100% in cost.

## 2. Indexes and partitions (top fix per query, as `recommend()` reports it)
Command: `python p3_evidence.py indexes`. `rl.recommend()` with `ai/rl_agent.json`, real `cost_fn` (HypoPG for indexes,
partitioned sandbox copy for `PARTITION ... MONTHLY`), masked `table_stats()`, and `write_fn` =
Person 1 `measure_write_latency()` for the final index fix. Drop / write cost = observed range (section 6).

| Query | Split | Top fix (masked) | Planner cost drop (range) | Measured gain (this run) | Measured write cost (range, insert time) | verified_by | Novelty |
|---|---|---|---:|---:|---:|---|---|
| q11 | held-out | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY; CREATE INDEX ON t_1c1 (c_0b8)` | 66.8% to 96.3% | 56.7% | not measured (partition) | `timed_run` | unseen shape (lower confidence) |
| q03 | held-out | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY; CREATE INDEX ON t_1c1 (c_073, c_02b)` | 96.0% to 98.2% | 96.3% | not measured (partition) | `timed_run` | seen query shape |
| q01 | train | `CREATE INDEX ON t_1c1 (c_1e3)` | 82.7% to 85.2% | 90.8% | +129% to +215% | `timed_run` | seen query shape |
| q02 | train | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 73.2% to 94.4% | 66.5% | not measured (partition) | `timed_run` | unseen shape (lower confidence) |
| q06 | held-out | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 83.8% to 87.3% | 77.3% | not measured (partition) | `timed_run` | seen query shape |
| q09 | train | `CREATE INDEX ON t_1c1 (c_1e3); CREATE INDEX ON t_1c1 (c_c3a)` | 92.9% to 93.3% | 99.3% | +54% to +340% | `timed_run` | seen query shape |
| q10 | train | `CREATE INDEX ON t_1c1 (c_073, c_c3a)` | 93.9% to 95.6% | 99.5% | +2% to +245% | `timed_run` | unseen shape (lower confidence) |
| q08 | train | `PARTITION t_1c1 BY RANGE (c_02b) MONTHLY` | 47.4% to 50.2% | 39.1% | not measured (partition) | `timed_run` | seen query shape |

q11 (the problem statement's example), `summary` (latest run): _Partition t_1c1 by c_02b and add index on t_1c1 (c_0b8) (57% faster in measured sandbox time (planner estimated 96%); write cost not measured). Zero raw data exposed to the AI (masked metadata only)._

q11, `why_not`: _Partitioning t_1c1 by c_02b not chosen: a smaller change does as well or better (simulated cost drop 95% vs 96% for the chosen fix)._

q01, `summary` with a measured write cost (latest run): _Add index on t_1c1 (c_1e3) (91% faster in measured sandbox time (planner estimated 85%), +0.00237 ms per inserted row, measured on the sandbox copy (+139% insert time)). Zero raw data exposed to the AI (masked metadata only)._

## 3. Bottleneck detection on the 11 real plans
Command: `python p3_evidence.py bottleneck`. `EXPLAIN (ANALYZE, FORMAT JSON)`, masked. Label = step with the highest
**measured** self time (actual time x loops minus children); none of the methods sees these timings.
Raw GNN / gated hybrid use the local `ai/gnn_weights.pt` (validated = False).
Skipped (original query runs longer than 60 s on 5M rows, so no measured label): q07.

| Query | Measured label (masked) | baseline | rules | raw GNN | gated hybrid |
|---|---|---|---|---|---|
| q01 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) |
| q02 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (96%) | OK Seq Scan t_1c1 (96%) | OK Seq Scan t_1c1 (96%) | OK Seq Scan t_1c1 (96%) |
| q03 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) |
| q04 | Seq Scan t_1c1 | MISS Index Scan t_21e (0%) | OK Seq Scan t_1c1 (100%) | OK Seq Scan t_1c1 (100%) | MISS Index Scan t_21e (0%) |
| q05 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) |
| q06 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) | OK Seq Scan t_1c1 (98%) |
| q08 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) | OK Seq Scan t_1c1 (94%) |
| q09 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) |
| q10 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) | OK Seq Scan t_1c1 (99%) |
| q11 | Seq Scan t_1c1 | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) | OK Seq Scan t_1c1 (92%) |

**Headline (strict measured labels, this run):** baseline **9/11**, rules **10/11**, raw GNN **10/11**, gated hybrid **9/11**.

(1) Parallel plan: time x loops adds the workers together and decides the label; counting workers once
would pick the step above. The agreed formula was kept. Flags and some labels change between runs
(e.g. q02 and q11's measured label moved between runs today), so treat this table as noisy.

(2) q07: Postgres books the hashed `NOT IN` list's time to the parent scan, so the strict label is the
parent and every method misses it. With q07 labelled as the subquery's scan: baseline 9/11, rules 10/11, raw GNN 10/11, gated hybrid 9/11.

### 3b. GNN attempts on real workload plans (why `rules` stays the default)
Dev-only experiments (scratch weights; the shipped `ai/gnn_weights.pt` is unchanged). Real
`EXPLAIN ANALYZE` plans of Person 1's workload, masked; split by held-out query template.

| Attempt | Test | Planner/baseline | Rules | GNN |
|---|---|---:|---:|---:|
| Slowest step, measured labels | held out t09-t12 (100) | 100/100 | 100/100 | 100/100 |
| Slowest step, measured labels | held out t08 + q04 (22) | 0/22 | **22/22** | 0/22 |
| Time share per step (787 plans, 4 planner settings) | held out t09-t12 (400): share error / slowest step | **10.8%** / 385 | - / 385 | 17.0% / 334 |
| Time share per step | held out t08 (35) | 98% / 0 | - / **35** | 99.5% / 0 |

Why: the labels are not varied. Even with planner knobs (hash/merge joins off, small
memory, no index scans, no parallel, no hash aggregate: 1,371 plans) 92% of bottlenecks are
the same big-table scan, so there is little for a GNN to learn. Decision (CLAUDE.md v3):
`rules` stays the default; the GNN is shown as `second_opinion` next to the reason.

## 4. Generalization: queries the agent never trained on
### 4a. Person 1's workload: 4 held-out templates, 100 queries
Command: `python workload_rl.py` (train + evaluate) or `--eval-only` (evaluate the saved agent).
Agent trained on 100 workload queries (templates t01-t08); tested on templates t09-t12 (25 queries each),
all masked. Reward = cost drop minus 0.03 per index column; a partition the sandbox refuses = no gain.

| Template | RL alone | RL + top-3 verify | random | index-everything | best possible | cost_fn calls (verify / best) |
|---|---:|---:|---:|---:|---:|---:|
| t09 | 0.930 (96%) | **0.930 (96%)** | 0.930 (96%) | 0.930 (96%) | 0.930 (96%) | 2 / 2.0 |
| t10 | 0.847 (89%) | **0.894 (94%)** | 0.522 (57%) | 0.567 (66%) | 0.911 (98%) | 4 / 9.0 |
| t11 | 0.779 (81%) | **0.914 (94%)** | 0.740 (78%) | 0.749 (81%) | 0.935 (98%) | 4 / 5.0 |
| t12 | 0.854 (88%) | **0.854 (88%)** | 0.148 (19%) | 0.176 (30%) | 0.854 (88%) | 4 / 8.0 |
| ALL | 0.852 (88%) | **0.898 (93%)** | 0.585 (62%) | 0.605 (68%) | 0.907 (95%) | 4 / 6.0 |

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
| RL alone | 95.8% | 87.3% | 87.1% | 0.871 |
| RL + top-3 verify | 95.8% | 87.3% | 87.1% | 0.871 |
| random | 60.4% | 21.6% | 24.1% | 0.315 |
| index-everything | 72.8% | 20.8% | 37.2% | 0.346 |
| best possible | 95.8% | 87.3% | 87.1% | 0.871 |

## 5. Before / after Person 1's sandbox
Before = `RESULTS.md` at commit `eac48d7` (indexes only, no partitions, agent trained on 8 queries).

| Item | Before | After |
|---|---|---|
| q11 top fix | index, 51.5% (`hypopg_cost`) | partition, 66.8% to 96.3% (`timed_run`) |
| q03 top fix | index, 85.9% (`hypopg_cost`) | partition, 96.0% to 98.2% (`timed_run`) |
| q02 top fix | index, 32.0% (`hypopg_cost`) | partition, 73.2% to 94.4% (`timed_run`) |
| q06 top fix | index, 0.1% (`hypopg_cost`) | partition, 83.8% to 87.3% (`timed_run`) |
| q08 top fix | partition, 0.0% (`rule_only`) | partition, 47.4% to 50.2% (`timed_run`) |
| q04 rewrite speedup | 10.2x (own timer) | 16.0x (`checked_run`) |
| q07 rewrite speedup | 2.2x (own timer) | >45.7x (`checked_run`) |
| q05 rewrite speedup | 2.4x (own timer) | 3.1x (`checked_run`) |
| q03/q06/q11 avg reward, RL + top-3 verify | 0.408 | 0.871 |
| q03/q06/q11 avg reward, RL alone | 0.153 | 0.871 |
| Agent training data | 8 real queries, 18 simulations | 100 workload queries, 277 simulations |
| Unseen-template test | none | 100 queries, 4 templates (section 4a) |
| Write cost in summary | width estimate ("estimated") | measured per inserted row (indexes) |
| Stats | read by Person 3's harness | Person 1 `table_stats()` |

## 6. Run-to-run variance (why some numbers are ranges)
Observed today (earlier runs + every fresh run of section 2):

| Number | Observed values | Conservative headline |
|---|---|---|
| q11 partition cost drop | 73.9%, 73.9%, 73.9%, 92.1%, 91.2%, 91.2%, 96.3%, 92.2%, 84.6%, 92.2%, 66.8%, 91.2% | **66.8%** |
| q02 partition cost drop | 73.2%, 73.2%, 73.2%, 91.9%, 91.2%, 91.2%, 94.4%, 92.0%, 92.0%, 92.0% | **73.2%** |
| q10 index write cost (insert time) | +203%, +97%, +53%, +123%, +146%, +94%, +245%, +58%, +73%, +120%, +2%, +66% | **+245%** |
| q01 index write cost (insert time) | +161%, +156%, +215%, +129%, +145%, +180%, +139%, +210%, +180%, +129% | **+215%** |
| q09 index write cost (insert time) | +340%, +188%, +177%, +105%, +54%, +127%, +91%, +212%, +231% | **+340%** |

Why: each time the sandbox's partitioned copy is rebuilt (teammates / `generate.py` drop stale copies),
Person 1's code runs `ANALYZE`, which **samples rows at random**, so the planner's statistics and cost
estimates change slightly; q11 moves between about -67% and -92%. Within one build the cost is stable.
Write latency is a real timing of small insert batches on a shared machine, so it is noisy too.
Index costs (HypoPG) did not vary.

## 7. Limits: read before quoting any number
- **Planner estimates vs measured time:** sections 2 and 4 use planner costs (HypoPG / partitioned
  copy). Section 0 measures the winners and shows how far the planner is off (trust ratio).
  Quote section 0 for real speedups.
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
- Small synthetic dataset (5M rows in the largest table, one machine).

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
| demo_q11_recorded.json | 137 | everyday word | everyday-word use |
| demo_q11_recorded.json | 164 | everyday word | everyday-word use |
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
| gnn.py | 336 | everyday word | everyday-word use |
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
| README.md | 36 | everyday word | everyday-word use |
| README.md | 156 | everyday word | everyday-word use |
| RESULTS.md | 204 | sales name | un-masked section (allowed) |
| RESULTS.md | 205 | everyday word | un-masked section (allowed) |
| RESULTS.md | 206 | sales name | un-masked section (allowed) |
| RESULTS.md | 207 | sales name | un-masked section (allowed) |
| RESULTS.md | 208 | sales name | un-masked section (allowed) |
| RESULTS.md | 209 | sales name | un-masked section (allowed) |
| RESULTS.md | 210 | sales name | un-masked section (allowed) |
| rewrite.py | 42 | everyday word | everyday-word use |
| rewrite.py | 138 | everyday word | everyday-word use |
| rewrite.py | 144 | everyday word | everyday-word use |
| rewrite.py | 172 | everyday word | everyday-word use |
| rewrite.py | 180 | everyday word | everyday-word use |
| rl.py | 123 | everyday word | everyday-word use |
| rl.py | 127 | everyday word | everyday-word use |
| rl.py | 344 | everyday word | everyday-word use |
| rl.py | 380 | everyday word | everyday-word use |
| rl.py | 704 | everyday word | everyday-word use |
| rl.py | 706 | everyday word | everyday-word use |
| rl.py | 709 | everyday word | everyday-word use |
| rl.py | 711 | everyday word | everyday-word use |
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
