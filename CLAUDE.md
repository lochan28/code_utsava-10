# Code Utsava X.0 — PS4: AI Database Tuning with Data Privacy

24-hour hackathon, 4-person team. Goal: a working, demo-ready prototype that
beginners can explain to judges. Keep code simple and minimal.

All code and the database live on ONE laptop (the team lead's). Each team
member drives their own Claude chat; each chat works in its **own git
worktree on its own branch** and edits **only its own folder**.

## What we are building
An AI dashboard that finds slow PostgreSQL queries and recommends fixes
(indexes, partitioning, SQL rewrites) WITHOUT the AI ever seeing raw data.
The AI only sees masked/hashed metadata.

Demo: admin asks "Why is the weekly sales report slow?" → slow query found
→ query + plan masked → GNN flags the bottleneck → RL picks the best fix →
HypoPG simulates it → local LLM explains in plain English → dashboard shows
before/after cost and time, write-latency and storage impact, "0 raw values
exposed", and an Approve button that outputs the SQL script.

Note: the official problem statement has a stray bullet about "capturing live
camera frames of passing billets". It is a copy-paste error — **ignore it.**

## Requirements from the official problem statement (agreed)
1. Demo query mirrors the PS example: `orders` has `region_id`; query `q11`
   "Weekly sales report by region"; expected fix = composite index
   `(region_id, order_date)` + monthly partitioning.
2. Privacy = **hashing** of names AND **attribute bitmasking** (per query, a
   bitmask of which columns are filtered / joined / grouped). 100% of raw
   names and literal values masked before any AI step.
3. **Ingest** slow-query logs (pg_stat_statements) AND historical execution
   plans (auto_explain).
4. Simulate impact on **write latency AND storage** (HypoPG index size).
5. **Robustness:** RL must work on **unseen** queries — train on generated
   query variants, test on held-out ones. Not just our fixed list.
6. **>50% improvement** in real execution time on inefficient joins: search
   with HypoPG cost, then confirm the winner by timing it on a sandbox copy.
7. Every recommendation has a human-readable, GNN-backed reason + the
   metadata evidence; admin reviews before "deployment" (Approve button
   produces the SQL script; it never runs it on live tables).

Known limits (put in README): HypoPG cannot fake partitions → partitioning is
simulated on a partitioned copy in the `sandbox` schema. Sharding needs
multiple servers → partitioning is presented as its single-machine version.

## Owners, folders, branches
| Person | Module | Folder | Branch |
|---|---|---|---|
| 1 | DB + Sandbox (+ ingestion, query workload generator) | `db/` | `db` |
| 2 | Obfuscation (hashing, bitmask, un-mask, leak check) | `privacy/` | `privacy` |
| 3 | RL + GNN + rule-based rewrites | `ai/` | `ai` |
| 4 | Dashboard + LLM + `pipeline.py` wiring | `app/`, `pipeline.py` | `app` |

Edit only your own folder. Need a change in someone else's module or in the
contract below? Ask that person / the team lead — don't edit it yourself.

## Shared interface contract — do NOT change without telling the team
Every module must work alone, with small sample data, and its file must run
directly (`python -m <module>`) as a self-test. `stub`s exist for every
function so others can build against them; replace the stub body, keep the
signature and the return shape.

Items marked **(v2)** were added on day 1 after the first integration (P3's
requests). They are additions only — nothing existing was renamed or removed.

`db/sandbox.py` (Person 1) — the only code that touches real SQL and data
- `get_plan(sql) -> dict` — `EXPLAIN (FORMAT JSON)` plan (no ANALYZE).
- `cost(sql, hypo_indexes=[]) -> float` — planner total cost, with HypoPG
  hypothetical indexes (list of `CREATE INDEX ...` strings). Never creates real indexes.
  **(v2)** The list may also contain ONE partition action, exact format
  `PARTITION <table> BY RANGE (<column>) MONTHLY` (MONTHLY only; `YEARLY` is not
  built); it is costed on a partitioned copy in its own schema `part_<table>_<column>`.
  Anything it cannot simulate → `ValueError`.
- **(v2)** `checked_run(sql, index_sqls=[]) -> dict` — `{"ms", "rows", "checksum"}`:
  median real ms on sandbox copies, row count, and an order-independent checksum of
  ALL result rows (`md5(string_agg(md5(t::text), '' ORDER BY md5(t::text)))`).
  Proves a rewrite returns the same rows AND is faster. `timed_run` stays as is.
- **(v2)** `table_stats(sql=None) -> dict` — REAL names, masked later by P2. Read
  from Postgres's own statistics (no table data):
  `{"rows": {table: n},`
  ` "eq_sel": {"table.col": share of rows matching ONE value (= 1/n_distinct)},`
  ` "range_sel": {"table.col": planner's share of rows for THIS query's range filter},`
  ` "existing_indexes": [(table, (cols,)), ...]}`
  `range_sel` is filled only when `sql` is given (else `{}`); `existing_indexes`
  includes primary keys. (Agreed day 1: this replaces the earlier `n_distinct` draft.)
- `index_size_bytes(index_sql) -> int` — HypoPG estimated size (storage impact).
- `measure_write_latency(index_sql) -> dict` — `{"before_ms", "after_ms", "delta_ms"}`
  per insert, measured on a copy table in schema `sandbox`.
- `timed_run(sql, index_sqls=[]) -> float` — real execution ms on a sandbox
  copy with those indexes really built (used only to confirm the final pick).
- `slow_queries(limit=10) -> list[dict]` — from pg_stat_statements:
  `{"query_id", "sql", "calls", "mean_ms"}`.

`privacy/mask.py` (Person 2)
- `build_mapping() -> Mapping` — from the DB schema; private, never given to AI.
- `mask_query(sql, mapping) -> str` — names → `t_xxx.c_xxx`, literals → `?`.
  **(v2)** Must return complete, valid SQL (parseable by sqlglot). Literals become
  numbered placeholders `$1, $2, ...`; the same value reuses the same number.
  SQL keywords inside strings are NOT masked: date units (`'month'`, `'week'`, ...)
  stay as they are.
- `mask_plan(plan, mapping) -> dict` — same masking on a plan JSON.
- `column_bitmask(sql, mapping) -> dict` — `{"filter": "0101...", "join": ..., "group": ...}`.
- **(v2)** `column_types(mapping) -> dict` — `{"t_x.c_y": "date"|"number"|"text"}`.
- **(v2)** `mask_stats(stats, mapping) -> dict` — masks the names in
  `sandbox.table_stats()` output, same shape. All masking lives in `privacy/`.
- `unmask(text, mapping) -> str` — only for the final display to the admin.
  **(v2)** Works on any text, including P3's `reason`, `why_not`, `summary`.
- `verify_no_leak(text, mapping) -> bool` — True if no raw name/value remains.

Privacy notes (put in README): numbered placeholders reveal that two values are
EQUAL, never what they are. Row counts, selectivities and result checksums are
metadata (numbers / hashes), not data.

`ai/` (Person 3) — sees ONLY masked data
- `ai/gnn.py: find_bottleneck(masked_plan) -> dict` —
  `{"node_id", "op", "reason", "evidence": {...}}`.
- `ai/rewrite.py: rewrite_sql(masked_sql) -> list[dict]` — `{"sql", "rule"}`.
- `ai/rl.py: recommend(masked_sql, masked_plan, cost_fn) -> list[dict]` —
  each `{"kind": "index"|"partition"|"rewrite", "action": <masked SQL>,
  "cost_before", "cost_after", "reason"}`, best first. `cost_fn(masked_sql,
  masked_index_sqls) -> float` is handed in by the pipeline; it un-masks
  privately and calls `db.sandbox.cost`, so the AI only ever gets a number back.
  **(v2)** Full signature: `recommend(masked_sql, masked_plan, cost_fn, stats=None,
  time_fn=None, agent=None, column_types=None)`. Extra keys per recommendation
  (`improvement_pct`, `verified_by`, `novelty`, `why_not`, `summary`, ...) are
  documented in `ai/README.md`. `stats` arrives in the `table_stats` shape above
  (masked): use `eq_sel` and `range_sel` directly. If a column is missing from
  `range_sel`, fall back to estimating from the masked plan + `stats["rows"]`.
- **(v2) OPEN — needs a team decision:** the PS asks for a *GNN-generated* reason,
  but `find_bottleneck` defaults to the `rules` path (GNN not yet validated).
  Decide how the GNN is shown in the demo before the final merge.

`app/llm.py` (Person 4): `explain(masked_result: dict) -> str` via Ollama
(`qwen2.5:7b`, `keep_alive=-1`), given ONLY masked data.

`pipeline.py` (Person 4): `analyze(question: str) -> dict` with keys:
`query_id, masked_query, masked_plan, bitmask, bottleneck, recommendations,
cost_before, cost_after, improvement_pct, time_before_ms, time_after_ms,
write_latency_ms, storage_bytes, explanation, deploy_sql, leak_check`.
`deploy_sql` is UN-masked (for the admin only); everything given to `ai/` and
`app/llm.py` is masked.
**(v2)** `analyze()` passes into `rl.recommend()`:
- `cost_fn(masked_sql, masked_actions)` — un-mask both, call `sandbox.cost`
  (handles indexes AND the partition action; lets `ValueError` propagate).
- `time_fn(masked_sql)` — un-mask, call `sandbox.checked_run`, return its dict.
- `stats = mask.mask_stats(sandbox.table_stats(real_sql), mapping)` — pass the
  REAL query so `range_sel` is filled.
- `column_types = mask.column_types(mapping)`

## Merge order (lead merges into master)
1. P2 masker (unblocks everyone)  2. P1 sandbox (+ `checked_run`, `table_stats`,
partition)  3. P3 ai (commit README + `gnn_weights.pt` + `rl_agent.json` first)
4. P4 app + pipeline.  After each merge, everyone runs `git merge master`.

## Shared database — careful
One Postgres in Docker serves all four chats.
- Start: `docker compose up -d` (Postgres 16 + HypoPG + pg_stat_statements).
- DSN: `postgresql://tuner:tuner@localhost:5432/sales` (local test credentials).
- **Only Person 1 runs `db/generate.py`** — it DROPS and rebuilds all tables,
  breaking everyone else mid-test. Others: read-only queries + HypoPG only.
- Experiments that need real tables/indexes go in schema `sandbox`, never `public`.

## Setup (once)
```
docker compose up -d
py -3.12 -m venv .venv          # already done on the lead's laptop
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python db\generate.py      # Person 1 only
.venv\Scripts\python db\queries.py       # lists the slow queries
.venv\Scripts\python pipeline.py         # end-to-end self-test
```
Add new packages to `requirements.txt` and tell the team. Torch / PyG are
Person 3's to add (GPU: RTX 5060 laptop, 8 GB).

## How we work (rules for every chat)
- Small steps: write one small piece → run it and show it works → explain in
  short, simple words → next piece. Never build several steps ahead.
- Tunable values go in a clearly marked block at the top of each file, each
  with a comment saying what raising/lowering it does. Keep fixed values
  (DSN, schema names) in a separate block.
- Don't start new work, experiments, or big changes without the user's OK;
  propose and wait.
- **Never `git commit` without asking first.** Never push.
- Ask before installing system packages or deleting anything.
- Never put real names/values into anything sent to `ai/` or the LLM.
