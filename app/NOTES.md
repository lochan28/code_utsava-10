# App notes (Person 4: dashboard, LLM, pipeline)

Everything below is OUR side of the contract. Items marked **LEAD** need the lead's eyes.

## 1. Extra keys `analyze()` returns (additions only; read them with `.get()`)
| key | what it holds |
|---|---|
| `match` | how the question was matched: `{query_id, name, confidence, method, runner_up}` |
| `llm` | how the explanation was made: `{source, prompt_leak_ok, reply_leak_ok, latency_ms, fallback_reason}` |
| `unsafe_rewrite` | `True` if any rewrite returned different rows than the original query |
| `ai_inputs` | which optional inputs `ai/` really got: `{time_fn, stats, column_types}`, each `"used"`, `"not available yet"`, `"not supported by ai/ yet"`, `"dropped: failed the leak check"`, `"unavailable"` or `"failed: rewrites are unverified"` |
| `explanation_unmasked` | **LEAD: pending OK, not built yet.** Explanation with real names put back, for admin display only; produced AFTER the LLM call; never fed back into any prompt |

Extra keys on each recommendation (set by the pipeline, only when it applies):
`unsafe_rewrite: True` and `warning` on a rewrite that changes the result. Such a rewrite is
sorted below every safe fix, so it can never be the "best" one.

## 2. Contract changes and where we differ from CLAUDE.md v2 (**LEAD: please read**)
1. **`llm.explain(masked_result, leak_check) -> dict`** (contract: `explain(masked_result) -> str`).
   Only `pipeline.py` calls it; `analyze()["explanation"]` is still a string. `leak_check(text)->bool`
   is passed in so `llm.py` never sees the private name mapping.
2. **`cost_fn` only costs `CREATE INDEX ...`.** Any other action (e.g. `PARTITION ...`, several
   statements) raises `ValueError`, which P3 turns into `rule_only`. v2 says `cost_fn` should also
   pass the partition action to `sandbox.cost`; it is one regex (`SIMULATABLE_ACTION` in
   `pipeline.py`) to allow it once Person 1's sandbox can cost partitions.
3. **`time_fn` returns `{"ms", "results_match"}`, NOT `{"ms", "rows", "checksum"}`.** The real run's
   rows and checksum are compared inside `pipeline.py` (against the original query) and never go to
   `ai/`. This means no value derived from real result rows reaches `ai/`, so there is nothing to
   approve about that. **It breaks P3's `ai/verify.py`** (reads `rows` and `checksum` directly).
4. **`stats` is completed by the pipeline** after masking and the leak check: it adds `eq_sel`
   (= 1 / `n_distinct`; negative `n_distinct` means a fraction of the rows), `range_sel = {}`, and
   empty `rows` / `existing_indexes` if missing. P3's `rl.py` indexes `stats["eq_sel"]` and
   `stats["range_sel"]` directly, but v2's `table_stats()` provides `n_distinct`, not `eq_sel`.
   Values already supplied are never overwritten.
5. **`rl.recommend()` is called with `time_fn` / `stats` / `column_types` only if it accepts them
   AND the supplying function exists** (`sandbox.checked_run`, `sandbox.table_stats`,
   `mask.mask_stats`, `mask.column_types`). Until then they are left out, no fake data. The
   current stub `recommend()` and P3's real one both work unchanged.
6. **Optional inputs never stop the analysis.** If stats / column_types fail the leak check or
   crash, they are dropped and an audit event is written. If the real timing run fails, the
   pipeline calls `recommend()` again without `time_fn` (P3's code does not catch that error).
7. **Strict privacy gate:** `PRIVACY_STRICT` (default `1`, below).

## 3. Audit log events (`app/audit_log.jsonl`, git-ignored, also in memory)
`llm_call`, `warmup`, `analysis_blocked`, `analysis_warned`, `stats_dropped`, `stats_unavailable`,
`column_types_dropped`, `column_types_unavailable`, `time_fn_failed`. Blocked or failed items are
logged as ids, sizes, a short hash and the exception TYPE only - never the text itself.

## 4. Settings read from environment variables
- `PRIVACY_STRICT` (default `1`): if masking leaves a real value behind, `analyze()` STOPS
  (admin sees "A value may not be masked, so the analysis was stopped for safety"). `0` = warn only.
  **LEAD: keep strict on for the demo?** (Person 4 recommends yes.)
- `OLLAMA_MODEL` (default `qwen2.5:7b`): if the 7b is too slow: `ollama pull qwen2.5:3b`, then
  `setx OLLAMA_MODEL qwen2.5:3b`, reopen the terminal.
- `OLLAMA_URL` (default `http://127.0.0.1:11434`): use 127.0.0.1, not `localhost` (on Windows
  `localhost` adds ~2 s to every request, measured).

## 5. How the LLM part behaves
- `llm.start_warmup()` (called at dashboard start) loads the model in a background thread.
- `llm.llm_status()` -> `{status: "live LLM" | "template fallback" | "warming up", reason, model}`.
- If Ollama is missing, slow (30 s) or returns something odd, the template explanation is used.
- The model's reply is untrusted text: only displayed, never executed, never used as SQL or as the
  Approve script. Fenced code blocks are removed from it before display.
- The prompt says values are replaced by `?` or `$1, $2, ...`.

## 6. Privacy rules we follow
- The raw question and pasted SQL are untrusted (may hold real names, emails, amounts). They are
  used only for matching and for the admin-only display - never in a prompt, the audit log or a
  cache key (`pipeline.cache_key` uses masked text only).
- Pasted SQL must be one read-only SELECT with no unknown functions.
- Text that fails a leak check is never stored in the audit log.

## 7. Tests (run from the project folder; none need a database or Ollama)
```
.venv/Scripts/python -m app.test_privacy          # privacy + guard
.venv/Scripts/python -m app.test_llm_http         # real Ollama call, against a fake server
.venv/Scripts/python -m app.test_dashboard        # the page (headless)
.venv/Scripts/python -m app.test_pipeline_hooks   # cost_fn, time_fn, stats, column_types
```

## 8. Asks for other people
- **Person 3:** `ai/verify.py` must read `results_match` instead of `rows` / `checksum`
  (see section 2, item 3). Also: `stats` now arrives with `eq_sel` already filled in.
- **Person 1 (db/sandbox.py):** pasted SQL reaches `get_plan()`, `cost()` and `checked_run()`. They
  must use plain `EXPLAIN` (never `EXPLAIN ANALYZE`) with a `statement_timeout`, in a read-only
  transaction. `checked_run()` is the only one that really executes SQL: sandbox schema only,
  read-only, with a timeout. The DB user `tuner` is a superuser; use a restricted read-only role.
