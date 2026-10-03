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

`db/sandbox.py` (Person 1) — the only code that touches real SQL and data
- `get_plan(sql) -> dict` — `EXPLAIN (FORMAT JSON)` plan (no ANALYZE).
- `cost(sql, hypo_indexes=[]) -> float` — planner total cost, with HypoPG
  hypothetical indexes (list of `CREATE INDEX ...` strings). Never creates real indexes.
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
- `mask_plan(plan, mapping) -> dict` — same masking on a plan JSON.
- `column_bitmask(sql, mapping) -> dict` — `{"filter": "0101...", "join": ..., "group": ...}`.
- `unmask(text, mapping) -> str` — only for the final display to the admin.
- `verify_no_leak(text, mapping) -> bool` — True if no raw name/value remains.

`ai/` (Person 3) — sees ONLY masked data
- `ai/gnn.py: find_bottleneck(masked_plan) -> dict` —
  `{"node_id", "op", "reason", "evidence": {...}}`.
- `ai/rewrite.py: rewrite_sql(masked_sql) -> list[dict]` — `{"sql", "rule"}`.
- `ai/rl.py: recommend(masked_sql, masked_plan, cost_fn) -> list[dict]` —
  each `{"kind": "index"|"partition"|"rewrite", "action": <masked SQL>,
  "cost_before", "cost_after", "reason"}`, best first. `cost_fn(masked_sql,
  masked_index_sqls) -> float` is handed in by the pipeline; it un-masks
  privately and calls `db.sandbox.cost`, so the AI only ever gets a number back.

`app/llm.py` (Person 4): `explain(masked_result: dict) -> str` via Ollama
(`qwen2.5:7b`, `keep_alive=-1`), given ONLY masked data.

`pipeline.py` (Person 4): `analyze(question: str) -> dict` with keys:
`query_id, masked_query, masked_plan, bitmask, bottleneck, recommendations,
cost_before, cost_after, improvement_pct, time_before_ms, time_after_ms,
write_latency_ms, storage_bytes, explanation, deploy_sql, leak_check`.
`deploy_sql` is UN-masked (for the admin only); everything given to `ai/` and
`app/llm.py` is masked.

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
