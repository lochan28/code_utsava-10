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

## 7. Before/after numbers on the page (Step 4c) - where each number comes from
Shown for the **top pick** (first by trust: measured, simulated, rule-only, unverified; never an unsafe one).
| number | key (contract v2) | source label on the page | notes |
|---|---|---|---|
| cost before / after, improvement % | `cost_before`, `cost_after` | planner estimate | improvement is worked out from the two costs. We do NOT use `improvement_pct`: ai/ makes it a TIME drop for rewrites |
| time before / after | `time_before_ms`, `time_after_ms` (top-level or on a `timed_run` rewrite) | measured | no measured time -> "Not measured, cost estimate only", never an empty chart |
| write latency | `write_latency_ms` | measured on a sandbox copy | missing -> "not measured", never 0 |
| storage | `storage_bytes` | measured on a sandbox copy (index really built there, then rolled back; HypoPG's guess was ~4x too big) | shown in MB (1024*1024); missing -> "not measured". A % needs the table size, which is not in the contract |
- Result-level numbers describe the pipeline's best fix (the first listed). They are used for the top pick
  ONLY if the top pick is that first one; otherwise they are not borrowed.
- A rewrite creates no index, so write latency and storage show "none: a rewrite creates no index" (by design, not a default).
- PS target: improvement strictly **more than 50%** (`tradeoff.TARGET_PCT`): green check if met, amber if not, red
  "WORSE" if the number is negative. Cost units and milliseconds are always on separate charts.
- If every fix is unsafe, only a warning is shown: no numbers, no charts, no green.
- **Everything on the stubs is fake** (cost 19437 -> 812, 27 -> 3 ms, 0.002 ms, 22,000,000 bytes). The numbers
  become real when Persons 1 and 3 deliver. Use the sidebar sample (4 variants) to see every state.

## 8. Demo mode and sample data
- The sidebar sample is OFF by default and can NEVER show while Demo mode is on. Demo mode itself is **not built
  yet (Step 5)**: it only has to set `st.session_state["demo_mode"] = True` and the guard already works.

## 9. Tests (run from the project folder; none need a database or Ollama)
```
.venv/Scripts/python -m app.test_privacy          # privacy + guard
.venv/Scripts/python -m app.test_llm_http         # real Ollama call, against a fake server
.venv/Scripts/python -m app.test_dashboard        # the page (headless)
.venv/Scripts/python -m app.test_pipeline_hooks   # cost_fn, time_fn, stats, column_types
.venv/Scripts/python -m app.test_recommendations  # badges, sorting, bottleneck card, sample / demo mode
.venv/Scripts/python -m app.test_before_after     # charts, 50% target, write / storage, trade-off
```

## 10. Asks for other people
- **Person 3:** `ai/verify.py` must read `results_match` instead of `rows` / `checksum`
  (see section 2, item 3). Also: `stats` now arrives with `eq_sel` already filled in.
- (4c) `table_stats` is now called with the SQL so Person 3 gets `range_sel`; `PARTITION ... MONTHLY` is passed to `cost`.
- **Person 1 (db/sandbox.py):** pasted SQL reaches `get_plan()`, `cost()` and `checked_run()`. They
  must use plain `EXPLAIN` (never `EXPLAIN ANALYZE`) with a `statement_timeout`, in a read-only
  transaction. `checked_run()` is the only one that really executes SQL: sandbox schema only,
  read-only, with a timeout. The DB user `tuner` is a superuser; use a restricted read-only role.
