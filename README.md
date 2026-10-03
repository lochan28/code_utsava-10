# AI Database Tuner with Data Privacy — Code Utsava X.0, PS4

An AI dashboard that finds slow PostgreSQL queries and recommends fixes
(indexes, partitioning, SQL rewrites) **without the AI ever seeing raw data**.
Team rules and the shared interface contract are in [CLAUDE.md](CLAUDE.md).

## Setup (lead's laptop, once)
Needs Docker Desktop (running) and Python 3.12.
```
docker compose up -d                                   # Postgres 16 + HypoPG + pg_stat_statements
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python db\generate.py                    # 1M orders in ~5 s (pass 5000000 for a bigger demo)
.venv\Scripts\python db\queries.py                     # the 11 slow queries: cost + real time
.venv\Scripts\python pipeline.py                       # end-to-end self-test
.venv\Scripts\streamlit run app\dashboard.py           # the dashboard
```

## Team workflow (4 chats, 1 laptop)
Each person gets their own **worktree** — a separate folder of the same repo
on their own branch — so the four Claude chats never overwrite each other:

| Person | Folder | Branch | Edits only |
|---|---|---|---|
| 1 DB + Sandbox | `C:\Code Utsav-db` | `db` | `db/` |
| 2 Privacy | `C:\Code Utsav-privacy` | `privacy` | `privacy/` |
| 3 RL + GNN | `C:\Code Utsav-ai` | `ai` | `ai/` |
| 4 Dashboard + LLM | `C:\Code Utsav-app` | `app` | `app/`, `pipeline.py` |

- All worktrees share the one venv: run Python as `"C:\Code Utsav\.venv\Scripts\python"`.
- All share the one database. Only Person 1 runs `db\generate.py` (it rebuilds every table).
- The lead merges branches into `master` when a piece works; everyone then
  merges `master` into their branch to pick up the others' work.

## Project layout
```
db/         generate.py (fake sales DB), queries.py (slow queries), sandbox.py (HypoPG cost, timings)
privacy/    mask.py (hashing, bitmask, un-mask, leak check)
ai/         gnn.py (bottleneck), rl.py (recommendations), rewrite.py (SQL rewrites)
app/        llm.py (Ollama explanations), dashboard.py (Streamlit)
pipeline.py analyze(question) -> dict : wires everything, enforces the privacy boundary
```

## Status
| Module | State |
|---|---|
| DB generator + 11 slow queries | working |
| sandbox, privacy, ai, llm | **stubs** (fake data, correct shape) |
| pipeline + dashboard | wired end-to-end on the stubs |

## Assumptions and limitations
- HypoPG simulates indexes only; partitioning is simulated on a partitioned
  copy of `orders` in the `sandbox` schema.
- Sharding needs several servers; partitioning is shown as its single-machine form.
- Data is synthetic (seeded, reproducible); no real customer data is used.

## Results
_To be filled in: before/after cost and time per query, write-latency and storage impact, leak-check result._
