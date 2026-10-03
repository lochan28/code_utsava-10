# App notes (Person 4: dashboard, LLM, pipeline)

## Extra keys we add: match, llm, explanation_unmasked (pending lead OK)
`pipeline.analyze()` returns everything in the CLAUDE.md contract, plus:
- `match` - how the question was matched: `{query_id, name, confidence, method, runner_up}`.
- `llm` - how the explanation was made: `{source, prompt_leak_ok, reply_leak_ok, latency_ms, fallback_reason}`.
- `explanation_unmasked` - **pending lead OK, not built yet.** The explanation with real
  names put back, for admin display only. It is produced AFTER the LLM call and must never
  be fed back into any prompt.

Adding keys breaks nobody. Anyone reading them should use `.get()`.

## One signature change inside our own code (LEAD: please note)
`app/llm.explain(masked_result, leak_check) -> dict` (the contract said `explain(masked_result) -> str`).
Only `pipeline.py` calls it, and `analyze()["explanation"]` is still a plain string.
`leak_check(text) -> bool` is passed in so `llm.py` never sees the private name mapping.
The dict has `text, source, prompt_leak_ok, reply_leak_ok, latency_ms, fallback_reason`.

## Settings read from environment variables
- `PRIVACY_STRICT` (default `1`): if masking leaves a real value behind, `analyze()` STOPS
  (admin sees "A value may not be masked, analysis stopped for safety"). `0` = warn only.
  Either way the event is written to the audit log (`analysis_blocked` / `analysis_warned`).
  Open question for the lead: keep strict on for the demo? (Person 4 recommends yes.)

## Privacy rules we follow
- The raw question and pasted SQL are untrusted: they may hold real names, emails, amounts.
  They are used only for matching and for the admin-only display - never in a prompt,
  the audit log or a cache key (`pipeline.cache_key` uses masked text only).
- Pasted SQL must be one read-only SELECT with no unknown functions.
- If masking leaves anything real behind, `analyze()` raises `PrivacyError` before the AI runs
  (see `PRIVACY_STRICT` above).
- Text that fails a leak check is never stored in `app/audit_log.jsonl` (git-ignored); only a marker + hash.
- Tests: `.venv/Scripts/python -m app.test_privacy`

## Asks for Person 1 (db/sandbox.py)
Pasted SQL reaches `get_plan()`, `cost()` and `timed_run()`. They must use plain `EXPLAIN`
(never `EXPLAIN ANALYZE`) with a `statement_timeout`, in a read-only transaction.
`timed_run()` is the only one that really executes SQL: sandbox schema only, read-only,
with a timeout. The DB user `tuner` is a superuser today; use a restricted read-only role.
