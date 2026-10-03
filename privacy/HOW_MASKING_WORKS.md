# How hashing and masking work (`privacy/mask.py`)

Real SQL, plans and statistics are masked **before** anything reaches `ai/` or
the LLM. Only the admin's final screen is un-masked.

```
real SQL / plan / stats ──► mask.py ──► masked text ──► GNN, RL, LLM
                               │                            │
                     private mapping (memory)   masked recommendation
                               └──────────► unmask() ──► admin screen
```

## 1. Keyed hashing of names — `build_mapping()`
1. Read every base table and column (with its data type) from
   `information_schema` in schema `public`. Views from HypoPG and
   pg_stat_statements are skipped.
2. Load the secret key from `C:\Code Utsav\.secret_key`. That's 32 random bytes in hex,
   kept outside every worktree and never committed. **A missing or empty
   file raises an error.** A new key is never created, because it would change every code.
3. Code each name as `prefix + HMAC-SHA256(key, prefix + name)[:3 hex]`:
   - tables → `t_438`, columns → `c_13b`
   - the same column name gets the same code in every table, so join keys line up
   - if two codes collide, all codes are lengthened by one character until they're unique
4. The result is a private `Mapping` object (real ↔ code), kept only in memory and never
   written to disk or passed to the AI.

**Why HMAC with a key, not a plain hash:** a plain `sha256("orders")` can be
recomputed by anyone who guesses the name. Without the key, the codes can't be
rebuilt. With the same key, the codes are identical on every run.

## 2. Masking a query — `mask_query(sql, mapping)`
The query is parsed with **sqlglot** (Postgres dialect), and the parsed query
is rewritten, not the text:

| Element | Becomes | Example |
|---|---|---|
| Table name | table code | `orders` → `t_438` |
| Column name | column code | `region_id` → `c_13b` |
| Table alias | `a1, a2…` (in order) | `o` → `a1` (a short alias hints at the name) |
| Output label | `x1, x2…` | `AS revenue` → `AS x4` |
| Literal value | `$1, $2…` (in reading order) | `3`, `'2026-09-01'` → `$1`, `$2` |
| Unknown name | `u_xxx` (keyed hash) | never passed through raw |
| Special strings | `$n` too | `E'..'`, `$$..$$`, `N'..'`, `B'..'`, `X'..'`, `U&'..'` |
| Comments | removed | `/* customer Diya */`, `-- note` |
| Kept as is | keywords, functions, `GROUP BY 1`, date units `'week'` | |

A **repeated value reuses its number**, so the AI sees that two values are equal
but not what they are. The output is still valid SQL (checked by re-parsing).

Example (q11):
```
raw:    ... FROM orders o JOIN regions r ON r.region_id = o.region_id ...
        WHERE o.region_id = 3 AND o.order_date >= '2026-09-01' AND c.segment <> 'small_business'
masked: ... FROM t_438 AS a1 JOIN t_b78 AS a2 ON a2.c_13b = a1.c_13b ...
        WHERE a1.c_13b = $1 AND a1.c_303 >= $2 AND a4.c_fdf <> $3
```

## 3. Masking a plan — `mask_plan(plan, mapping)`
EXPLAIN JSON is walked field by field:
- `Relation Name` → table code. `Alias` → the **same** `a1…` as the query.
- `Index Name` (including HypoPG names like `<13625>btree_orders_region_id`) → `i_xxx`.
- Text fields (`Filter`, `Hash Cond`, `Sort Key`, …) are often partial SQL, so
  they're masked token by token: names become codes, `'values'` and numbers become `$n`,
  reusing the query's numbers. Postgres's `'2026-09-01 00:00:00+00'` is treated as
  `'2026-09-01'`.
- Postgres vocabulary fields (`Node Type`, `Join Type`, …) and all **numbers**
  (cost, rows, width) are kept, because the GNN needs them.

## 4. Other masked outputs
- `column_bitmask(sql)` gives one bit per column for **filter / join / group**,
  plus a `columns` legend of masked names. Columns are resolved with sqlglot
  `qualify`. A column can be both joined and filtered (q11 `region_id`).
- `column_types()` maps each column to `date | number | text`, using masked names only.
- `mask_stats(stats)` masks the names in `sandbox.table_stats()` and keeps the numbers.
  **Any value that isn't a number is refused** with an error.

## 5. Un-masking — `unmask(text, mapping)`
Codes (`t_…`, `c_…`, `u_…`) are swapped back to real names. This is used **only** for
the admin's final screen and the deploy SQL. Values (`$n`) are never restored.

## 6. Leak check — `verify_no_leak(text, mapping, free_text=False)`
It searches as whole words, ignoring case, for:
- every real table and column name, and every literal value masked so far;
- any email address, and any 13–19 digit number that passes the Luhn card checksum.

Details:
- For JSON, only values are checked. Keys like `"Relation Name"` are not data.
- `free_text=True` is for LLM sentences. Plain-English column names
  (`status`, `name`) and 1–2 digit numbers are allowed. Table names, names with
  `_`, longer values, emails and card numbers still count.
- If a hidden value is a whole number, its occurrences that are `$n` placeholders or
  `GROUP BY` / `ORDER BY` positions are excused **only when sqlglot parses the text
  and confirms them**. Text that can't be parsed gets no exception.

## 7. Proof — `python -m privacy.mask`
- **11/11 demo queries:** the masked query, plan, verbose plan and bitmask are leak-free,
  and the masked query is valid SQL.
- **Control:** the raw query and plan are caught (so the checker isn't always "yes").
- `column_types`, `mask_stats`, and the 4 LLM-explanation cases (2 clean pass,
  email and card blocked): PASS.
- **Unseen queries** (P1's test split): 100/100 masked queries and plans are clean,
  and 100/100 raw queries are caught.

## 8. Limits
- The checker only finds names and values it has masked, plus the email and card patterns.
  Any other sensitive format needs its own pattern.
- `$n` shows that two values are equal (not what they are). Row counts and
  selectivities are shared as metadata.
- 3-character codes are fine for this schema. Raise `HASH_LEN` for a large schema.
