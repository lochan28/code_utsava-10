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
