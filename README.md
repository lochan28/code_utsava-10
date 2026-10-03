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
| DB generator, 11 slow queries, workload generator, ingestion | working (merged) |
| sandbox: HypoPG cost, partition simulation, timing, write/storage impact | working (merged) |
| privacy: masking, leak check | working (merged) |
| ai (RL, GNN, rewrites), app (LLM, dashboard) | in progress on their branches; master has stubs |

## Privacy: what the AI can and cannot see

Every query, plan and statistic is masked by `privacy/mask.py` **before** it
reaches the AI modules (`ai/`) or the LLM. Only the admin's final screen is
un-masked.

### Hidden vs kept
| Hidden (never reaches the AI) | Kept (the AI needs it to reason) |
|---|---|
| Table names → `t_438`, column names → `c_13b` | SQL keywords and functions (`JOIN`, `SUM`, `DATE_TRUNC`) |
| Table aliases (`o`, `c`) → `a1`, `a2`; output labels (`revenue`) → `x1` | Query shape: joins, filters, `GROUP BY 1` positions |
| Every literal value (ids, dates, `'pending'`) → `$1`, `$2` … | Date units (`'week'`, `'month'`): SQL vocabulary, not data |
| Index names (incl. HypoPG's) → `i_d3e` | Plan numbers: costs, row estimates, widths |

### Keyed hashing
Each name is hashed with HMAC-SHA256 and a secret key. Without the key, nobody
can rebuild a code by hashing a guessed name such as "orders". The key is kept at
`C:\Code Utsav\.secret_key`, outside every worktree, and is never committed.
A missing or empty key file **raises an error**. The module never makes a new key,
because a new key would silently change every code. The private name ↔ code table
is rebuilt in memory on each run and is never written to disk or given to the AI.

### Equality note ($n placeholders)
A repeated value reuses its number. For example, `status = $1 OR (status = $1 AND …)`
shows the AI that the two values are **equal**, but never what the value is.

### Metadata vs data
Row counts, distinct counts, selectivities, plan costs and result checksums are
**metadata**: numbers and hashes that describe the data's shape. They are
shared with the AI. Table contents and query values are **data**, and they are
never shared.

### Leak check
`verify_no_leak()` searches masked output for any real name or hidden value.
It also looks for email addresses and card numbers (13–19 digits that pass the
Luhn checksum). For LLM text it runs in a free-text mode, where ordinary words
such as "status" are allowed. To avoid false alarms, the numbers inside `$n`
placeholders and `GROUP BY 1` / `ORDER BY 1` positions are excused, but only
when sqlglot confirms them by parsing the text.

### Proof (`python -m privacy.mask`)
| Check, for each of the 11 demo queries | Result |
|---|---|
| Masked query, plan, verbose plan and bitmask contain no real name or value | **11/11 PASS** |
| Masked query is still valid SQL | **11/11 PASS** |
| Control: raw query and raw plan are caught by the checker | **11/11 PASS** |
| `column_types`, `mask_stats` output leak-free | **PASS** |
| LLM explanations: 2 clean ones pass, email and card number are blocked | **PASS** |

**Unseen queries** (P1's workload generator, held-out test split): masked queries
**100/100 clean**, masked plans **100/100 clean**, and raw queries **100/100 caught**.
The train split is also 100/100 clean.

**Limits:** the checker finds names and values that it has seen or that match a
pattern (email, card number). Other sensitive formats need their own pattern.

## Assumptions and limitations
- HypoPG simulates indexes only; partitioning is simulated on a partitioned
  copy of `orders` in its own schema (`part_orders_order_date`).
- Sharding needs several servers; partitioning is shown as its single-machine form.
- Data is synthetic (seeded, reproducible); no real customer data is used.

## Results
_To be filled in: before/after cost and time per query, write-latency and storage impact, leak-check result._
