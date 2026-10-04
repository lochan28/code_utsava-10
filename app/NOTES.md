# App notes (Person 4: dashboard, LLM, pipeline)

Everything below is OUR side of the contract. Items marked **LEAD** need the lead's eyes.

## 1. Extra keys `analyze()` returns (additions only; read them with `.get()`)
| key | what it holds |
|---|---|
| `match` | how the question was matched: `{query_id, name, confidence, method, runner_up}` |
| `llm` | how the explanation was made: `{source, prompt_leak_ok, reply_leak_ok, latency_ms, fallback_reason}` |
| `unsafe_rewrite` | `True` if any rewrite returned different rows than the original query |
| `ai_inputs` | which optional inputs `ai/` really got: `{time_fn, stats, column_types}`, each `"used"`, `"not available yet"`, `"not supported by ai/ yet"`, `"dropped: failed the leak check"`, `"unavailable"` or `"failed: rewrites are unverified"` |
| _(per recommendation, set by ai/)_ | bottleneck: `second_opinion` ({op, relation, confidence, agrees}, hidden when None) and `evidence.gnn_top3` (small bar chart). Fixes: `measured_gain_pct`, `measured_ms_before`, `measured_ms_after`, `planner_gain_pct`, `timing_note`, `rejected_fixes`, `combined`. The page shows the measured numbers as the headline, the planner estimate in a collapsed box, the note under the time chart, rejected fixes as a neutral note, and a combined fix (`PARTITION ...; CREATE INDEX ...`) as two steps |
| _(per recommendation, set by ai/)_ | also `write_latency` (`{source: measured|estimated, delta_ms_per_row, pct}`; only a measured one is used), `rewrite_sql` + `rewrite_rule` (the fix was timed together with a query rewrite), `combined_rewrite`. The page shows the rewrite under the fix, and the rule in the evidence list |
| `deploy_rewrites` | the un-masked query rewrite that goes with EACH recommendation (`""` if none), same order as `recommendations`. **Approve step only**, restored after the AI and the LLM are finished |
| `deploy_scripts` | the un-masked fix for EACH recommendation, same order as `recommendations` (real values restored). **Approve step only.** `deploy_sql` is the first one |
| `explanation_unmasked` | the explanation with the admin's real names put back. Built from the FINISHED explanation (after the LLM call), so it never reaches a prompt, the audit log or `ai/`. Shown ONLY inside the Approve section, after the click |
| `demo` | `True` only on results that Demo mode returned from the recording |

Extra keys on each recommendation (set by the pipeline, only when it applies):
`unsafe_rewrite: True` and `warning` on a rewrite that changes the result. Such a rewrite is
sorted below every safe fix, so it can never be the "best" one.

## 2. Contract changes and where we differ from CLAUDE.md v2 (**LEAD: please read**)
1. **`llm.explain(masked_result, leak_check) -> dict`** (contract: `explain(masked_result) -> str`).
   Only `pipeline.py` calls it; `analyze()["explanation"]` is still a string. `leak_check(text)->bool`
   is passed in so `llm.py` never sees the private name mapping.
2. **`cost_fn` costs one `CREATE INDEX ...` or one `PARTITION t BY RANGE (c) MONTHLY`** (`SIMULATABLE_ACTION`
   in `pipeline.py`). Anything else (YEARLY, several statements, DROP ...) raises `ValueError`, which P3
   turns into `rule_only`. For rewrites the real values are put back first (section 9).
3. **`time_fn(masked_sql, actions=())` returns `{"ms", "results_match"}`, NOT `{"ms", "rows", "checksum"}`.** `actions` = masked CREATE INDEX / one MONTHLY partition, applied on the sandbox copy before timing (restored to real names only there). A fix the sandbox cannot build raises `ValueError` (ai/ keeps its planner figure). The same (sql, actions) is run once: `checked_run` already takes a median of 5 runs, so ai/'s 5 repeated calls cost one run. The real run's
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
8. **`write_fn` (contract v3)**: `recommend(..., write_fn=)` gets `write_fn(masked CREATE INDEX) -> {before_ms, after_ms, delta_ms}` (one plain index only; names restored only for the sandbox; failures carry the error TYPE). **Un-masking is ours, in three marked places** (section 9). `ai/` only ever gets masked text; a rewrite is
   verified on the sandbox with the real values restored just before the call.
9. **Measured time is the headline** whenever a measured time exists; the planner estimate sits in a collapsed
   expander and may disagree (amber note, not red). Without a measured time the page shows the estimate.

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
- When a MEASURED time exists it is the headline (chart + the 50% target check). The planner estimate
  moves into a collapsed expander "Planner estimate (can disagree with measured time)". If the two point
  in opposite directions the estimate gets a neutral amber note, not a red WORSE. With no measured time
  the page is as before (cost estimate only).
- **Everything on the stubs is fake** (cost 19437 -> 812, 27 -> 3 ms, 0.002 ms, 22,000,000 bytes). The numbers
  become real when Persons 1 and 3 deliver. Use the sidebar sample (4 variants) to see every state.

## 8. Demo mode and sample data
- **Demo mode** (sidebar switch, sets `st.session_state["demo_mode"]`) answers from RECORDED real analyses:
  `app/demo_results.json`, one per demo query, made by `python -m app.record_demo` (needs the database; Ollama is
  not used, so the explanations are the template). In Demo mode no database, sandbox, Ollama call or Ollama
  warm-up happens (`app/test_demo_mode.py` rigs all of them to explode). Pasted SQL and unmatched questions get a
  plain message. A violet badge and a banner say "recorded results".
- The recording in the repo was made with Person 3's real agent (`ai` branch, commit `73ea204`) on the 5M-order dataset.
  **Re-record whenever `ai/`, the sandbox or the dataset changes** (about 5 minutes on 5M orders).
- The recording holds the demo queries' own literals in `deploy_scripts` / `deploy_rewrites` / `explanation_unmasked` (demo data, not
  private data). The Approve step works in Demo mode (it only shows text).
- The sidebar sample switch ("every badge type", made-up numbers) is OFF by default, and is HIDDEN while Demo mode
  is on: made-up numbers never show next to a demo.

## 9. Restoring real values (the `$1, $2` fix) - where it may and may not happen
The masker turns every value into `$1, $2` and keeps value -> number privately (`mapping.query_params`).
`pipeline._restorer` puts the real values back (text is quoted with `'` doubled, so a hostile value
cannot end its own quotes; numbers stay bare). It is used in exactly THREE places, each marked
`RESTORE POINT` in `pipeline.py`:
1. `make_cost_fn`: right before `sandbox.cost` (plain `EXPLAIN`, no execution);
2. `make_time_fn`: right before `sandbox.checked_run` (really runs the SQL on the sandbox copy);
3. the Approve scripts (`deploy_scripts` / `deploy_sql`), built AFTER the AI and the LLM are finished.
The restored text never goes into a prompt, the audit log, a cache key, `ai/`, or the result dict
(apart from `deploy_scripts` / `deploy_sql`). `app/test_restore.py` has a tripwire for this, and
proves it works: moving the restore before the AI or LLM call turns it red.
What the sandbox enforces today (read from `db/sandbox.py`): `cost()` / `get_plan()` use a READ-ONLY
connection with a 30 s timeout and plain `EXPLAIN`. `timed_run()` / `checked_run()` run the query
(`EXPLAIN ANALYZE`) with a 30 s timeout, `search_path = sandbox`, `SET transaction_read_only = on`
and a rollback at the end. Not airtight: the user is a superuser and a `SELECT ... INTO` still ran
inside the read-only run (rolled back). Our SQL guard in `pipeline.py` rejects `INTO` and unknown
functions first. **Person 1: please use a restricted role.**

## 10. The Approve step (the only place un-masked SQL appears)
- A button per analysis. Before the click the un-masked SQL is not on the page at all.
- After the click: a warning "NOT EXECUTED, review before running", the script in a code block (it has
  a copy button) and a "Download as .sql" button. The script is `BEGIN; ... ROLLBACK;`, so running it
  as it stands keeps nothing; the admin changes `ROLLBACK` to `COMMIT` only after reviewing.
- It is the script of the TOP PICK (not always the first fix listed), never of an unsafe fix. A
  partition has no executable script (it is a multi-step migration): the script is comments only.
- It only builds text (`app/approve.py`). `app/test_approve.py` rigs the sandbox, every database
  connection and the decoder to explode if the page touches them.

## 11. Tests (run from the project folder; none need Ollama)
```
.venv/Scripts/python -m app.test_privacy          # privacy + guard
.venv/Scripts/python -m app.test_llm_http         # real Ollama call, against a fake server
.venv/Scripts/python -m app.test_dashboard        # the page (headless)
.venv/Scripts/python -m app.test_pipeline_hooks   # cost_fn, time_fn, stats, column_types
.venv/Scripts/python -m app.test_recommendations  # badges, sorting, bottleneck card, sample / demo mode
.venv/Scripts/python -m app.test_before_after     # charts, 50% target, measured-time headline, trade-off
.venv/Scripts/python -m app.test_restore          # real values restored only for the sandbox + Approve (tripwire)
.venv/Scripts/python -m app.test_approve          # the Approve step never executes anything
.venv/Scripts/python -m app.test_demo_mode        # Demo mode: recorded answers, nothing live is touched
.venv/Scripts/python -m app.test_time_actions     # time_fn(masked_sql, actions): timing a fix on the sandbox copy
.venv/Scripts/python -m app.test_tail             # combined fixes, index-part-only write cost, Approve script for a combined fix
.venv/Scripts/python -m app.test_ai_fields        # the page shows ai/'s measured fields, rejected and combined fixes
.venv/Scripts/python -m app.test_gnn_fields       # GNN second opinion, top-3 chart, rewrite rule label
.venv/Scripts/python -m app.test_rewrite_fix      # a fix timed with a query rewrite: script, card, ai/'s write latency
```
(`test_restore` and `test_approve` use the real masker and read-only sandbox runs, so they need the Docker database.)

## 12. Decisions we made (for the lead)
- Real values come back only for the sandbox run and the Approve text (section 9). Text values are quoted with `'`
  doubled; numbers stay bare (the type is read from the original SQL because the masker does not record it).
- Approve only SHOWS a script (`BEGIN; ... ROLLBACK;` with real statements, "NOT EXECUTED"), plus copy and
  download. It never runs anything. One script per recommendation (`deploy_scripts`); the page shows the top pick's.
- A partition has no executable script (it is a multi-step migration): comments only. A combined fix adds its CREATE INDEX as a real statement. A query rewrite that was timed with the fix is written as COMMENTS ("replace the query in your application with this one"): it is a code change, not something to run.
- The script text is hardened against newlines: everything inside a comment is one line, an index must be one line, and a "rewrite" is a statement only if it is exactly one plain SELECT (else comments). Found by a test; it existed since 4d.
- Stats and column types are checked with their KEYS too, because `verify_no_leak` ignores JSON keys.
- The planner and the stopwatch can disagree: in the recording q07's rewrite is 87% faster measured while the
  planner cost says 23% worse. The page leads with the measured time and shows the estimate in a collapsed box
  with an amber note (not a red WORSE).
- The top-level numbers of the result (`cost_*`, `time_*_ms`, `improvement_pct`) describe `recommendations[0]`;
  the page's top pick (best evidence tier) is not always that one, so read per-recommendation fields
  (`improvement_pct`, `verified_by`) when you need the top pick's own numbers.

## 13. Asks for other people
- **Lead:** merge the `ai` branch into master (master still has the 3-argument stub `rl.recommend`), then re-run
  `python -m app.record_demo` so the Demo mode recording matches what is merged.
- **Person 3:** `ai/verify.py` already reads `results_match` (done). `stats` arrives with `eq_sel` filled in. `pipeline.py` stays Person 4's (the leak checks are in `analyze()`). `time_fn(masked_sql, actions)`, `cost_fn` (costs the SQL it is given) and `write_fn` are wired. The ai_inputs words "not supported by ai/ yet" only appear when `recommend()` really lacks the parameter. A fix that is dropped should keep going into `rejected_fixes` as masked text.
- **Person 2:** `mask_query` turns an existing `$1` into `$$1` and then flags it as a leak (any logged query or pasted SQL that already has `$n`). Fix: leave existing `$n` placeholders alone.
- (4c) `table_stats` is now called with the SQL so Person 3 gets `range_sel`; `PARTITION ... MONTHLY` is passed to `cost`.
- **Person 1 (db/sandbox.py):** pasted SQL reaches `get_plan()`, `cost()` and `checked_run()`. They
  must use plain `EXPLAIN` (never `EXPLAIN ANALYZE`) with a `statement_timeout`, in a read-only
  transaction. `checked_run()` is the only one that really executes SQL: sandbox schema only,
  read-only, with a timeout. The DB user `tuner` is a superuser; use a restricted read-only role.
