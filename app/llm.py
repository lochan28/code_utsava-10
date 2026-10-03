"""Plain-English explanation from the local LLM (Ollama, qwen2.5:7b).

Owner: Person 4.  STATUS: Step 2 - prompt, leak checks, audit log and template
fallback are real.  The Ollama call itself is NOT wired yet (Step 3), so every
call currently falls back to the template.

Privacy rules this file enforces:
  * the prompt is built ONLY from masked data (never the question or raw SQL);
  * the prompt is leak-checked BEFORE it is sent; if it fails, nothing is sent;
  * the LLM's reply is leak-checked too; if it fails, it is thrown away;
  * every call is written to an audit log (memory + audit_log.jsonl). Text that
    failed a check is NOT stored - only a marker and a hash - so the log can
    never become a copy of the leak.

This file never sees the private name mapping. The pipeline hands in a
`leak_check(text) -> bool` function (True = clean) that already knows it.

Self-test:  .venv/Scripts/python -m app.llm
"""
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# TUNABLE SETTINGS
# ============================================================
MAX_PLAN_NODES = 6    # Plan steps shown to the LLM (most expensive first).
                      # More = more context but a longer prompt, so slower replies.
MAX_RECOMMENDATIONS = 3   # Fixes shown to the LLM and used in the fallback text.
                          # The LLM only needs the best few.
TIMEOUT_S = 60        # (Used from Step 3.) Give up on Ollama after this long.
                      # Lower = demo recovers faster when Ollama hangs.

# ============================================================
# FIXED SETTINGS
# ============================================================
MODEL = "qwen2.5:7b"
OLLAMA_URL = "http://localhost:11434/api/generate"
AUDIT_LOG_PATH = Path(__file__).with_name("audit_log.jsonl")   # git-ignored

PROMPT_TEMPLATE = """You are a database performance assistant for an administrator.
You only see anonymised metadata: table and column names are hashed (like t_a3f, c_91b)
and values are replaced by "?". Use only the names given below and never guess real ones.

Slow query (masked):
{masked_query}

Plan steps, most expensive first:
{plan_lines}

Bottleneck: {bottleneck}

Recommended fixes, best first:
{recommendations}

In 3 short sentences, for a non-expert: why the query is slow, what the best fix does,
and the benefit. Plain words, no jargon."""

SAFE_TEXT = ("A fix was found, but the written explanation was withheld because it "
             "failed the privacy check. See the recommendation table instead.")

AUDIT_LOG = []                    # same records as the file, kept in memory for the dashboard
_lock = threading.Lock()


class LLMUnavailable(Exception):
    """Ollama could not be used (not installed, not running, timed out)."""


# ------------------------------------------------------------------
# Building the prompt (masked data only)
# ------------------------------------------------------------------
def _num(x):
    return f"{x:,.0f}" if isinstance(x, (int, float)) else "?"


def _plan_lines(masked_plan):
    """Short list of the most expensive plan steps. Uses a fixed whitelist of fields
    on purpose: plan filters / index conditions can hold literal values."""
    steps, stack = [], [masked_plan.get("Plan", {})]
    while stack:
        n = stack.pop()
        if n:
            steps.append(n)
            stack.extend(n.get("Plans", []))
    steps.sort(key=lambda n: n.get("Total Cost") if isinstance(n.get("Total Cost"), (int, float)) else 0,
               reverse=True)
    lines = []
    for n in steps[:MAX_PLAN_NODES]:
        where = f" on {n['Relation Name']}" if n.get("Relation Name") else ""
        lines.append(f"- {n.get('Node Type', '?')}{where}: cost {_num(n.get('Total Cost'))}, "
                     f"~{_num(n.get('Plan Rows'))} rows")
    return "\n".join(lines) or "- (no plan available)"


def _pct(before, after):
    if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before > 0:
        return round(100 * (1 - after / before))
    return None


def _rec_lines(recs):
    lines = []
    for i, r in enumerate(recs[:MAX_RECOMMENDATIONS], 1):
        pct = _pct(r.get("cost_before"), r.get("cost_after"))
        gain = f", cost {_num(r.get('cost_before'))} -> {_num(r.get('cost_after'))}" + \
               (f" ({pct}% lower)" if pct is not None else "")
        lines.append(f"{i}. [{r.get('kind', '?')}] {r.get('action', '?')}{gain}. "
                     f"Reason: {r.get('reason', '?')}")
    return "\n".join(lines) or "(none)"


def _bottleneck_line(b):
    evidence = json.dumps(b.get("evidence", {}), sort_keys=True)
    return f"{b.get('op', '?')} - {b.get('reason', '?')} (evidence: {evidence})"


def build_prompt(masked_result):
    """The exact text sent to the LLM. Uses ONLY these four masked fields:
    masked_query, masked_plan (summarised), bottleneck, recommendations."""
    return PROMPT_TEMPLATE.format(
        masked_query=masked_result.get("masked_query", "?"),
        plan_lines=_plan_lines(masked_result.get("masked_plan", {})),
        bottleneck=_bottleneck_line(masked_result.get("bottleneck", {})),
        recommendations=_rec_lines(masked_result.get("recommendations", [])))


# ------------------------------------------------------------------
# Template fallback: same facts, written out by rules, no LLM
# ------------------------------------------------------------------
def template_explanation(masked_result):
    b = masked_result.get("bottleneck", {})
    recs = masked_result.get("recommendations", [])
    evidence = ", ".join(f"{k}={v}" for k, v in sorted(b.get("evidence", {}).items()))
    text = f"Why it is slow: {b.get('reason', 'the main cost could not be identified')}"
    text += f" (evidence: {evidence})." if evidence else "."
    if recs:
        best = recs[0]
        pct = _pct(best.get("cost_before"), best.get("cost_after"))
        text += f" Best fix: {best.get('action', '?')} - {best.get('reason', '')}"
        if pct is not None:
            text += (f" The planner's estimated cost drops from {_num(best.get('cost_before'))} "
                     f"to {_num(best.get('cost_after'))}, about {pct}% lower.")
        if len(recs) > 1:
            text += f" {len(recs) - 1} other option(s) were also considered."
    return text


# ------------------------------------------------------------------
# The LLM call (Step 3 fills this in)
# ------------------------------------------------------------------
def _call_ollama(prompt):
    """Send the prompt to Ollama and return its reply text.
    Raise LLMUnavailable if that is not possible."""
    raise LLMUnavailable("Ollama is not wired up yet (Step 3).")


# ------------------------------------------------------------------
# Audit log
# ------------------------------------------------------------------
def _redact(text):
    """Stand-in for text that failed a leak check (the text itself is never stored)."""
    return (f"[BLOCKED by leak check, not stored: {len(text)} chars, "
            f"sha256={hashlib.sha256(text.encode()).hexdigest()[:12]}]")


def _audit(record):
    with _lock:
        AUDIT_LOG.append(record)
        try:
            with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass   # a full disk must not break the demo; the in-memory log still has it


def get_audit_log():
    """Copy of this session's audit records, oldest first (for the dashboard tab)."""
    with _lock:
        return list(AUDIT_LOG)


def _is_clean(leak_check, text):
    """Fail closed: a checker that crashes counts as 'leak found'."""
    try:
        return bool(leak_check(text))
    except Exception:
        return False


# ------------------------------------------------------------------
# The one function the pipeline calls
# ------------------------------------------------------------------
def explain(masked_result, leak_check):
    """Explain a masked analysis in plain English.

    leak_check(text) -> bool  True means "no real name or value in this text".
    Provided by the pipeline, which owns the private mapping.

    Returns a dict:
      text            the explanation. ALWAYS passed leak_check (or is SAFE_TEXT).
      source          "ollama" or "template"
      prompt_leak_ok  did the prompt pass the check (False -> nothing was sent)
      reply_leak_ok   did the LLM's reply pass (None if no reply was received)
      latency_ms      total time spent in this function
      fallback_reason why the template was used (None when source is "ollama")
    """
    t0 = time.perf_counter()
    prompt = build_prompt(masked_result)
    prompt_ok = _is_clean(leak_check, prompt)

    reply, reply_ok, reason = None, None, None
    if not prompt_ok:
        reason = "prompt failed the leak check, nothing was sent"
    else:
        try:
            reply = _call_ollama(prompt)
        except LLMUnavailable as e:
            reason = f"LLM unavailable: {e}"

    text, source = None, "template"
    if reply is not None:
        reply_ok = _is_clean(leak_check, reply)
        if reply_ok and reply.strip():
            text, source = reply.strip(), "ollama"
        else:
            reason = "reply failed the leak check" if not reply_ok else "LLM returned an empty reply"

    if text is None:
        text = template_explanation(masked_result)
        if not _is_clean(leak_check, text):          # the facts themselves carry a leak
            text = SAFE_TEXT
            reason = (reason or "") + "; template text also failed the check"

    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    _audit({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "sent": reply is not None,
        "prompt": prompt if prompt_ok else _redact(prompt),
        "prompt_leak_ok": prompt_ok,
        "reply": None if reply is None else (reply if reply_ok else _redact(reply)),
        "reply_leak_ok": reply_ok,
        "latency_ms": latency_ms,
        "fallback_reason": reason,
    })
    return {"text": text, "source": source, "prompt_leak_ok": prompt_ok,
            "reply_leak_ok": reply_ok, "latency_ms": latency_ms, "fallback_reason": reason}


# ------------------------------------------------------------------
# Self-test: 3 analyses, the 3rd one deliberately leaky
# ------------------------------------------------------------------
def _sample(kind, action, reason, query="SELECT ... FROM t_a3f WHERE t_a3f.c_91b = ?"):
    return {"masked_query": query,
            "masked_plan": {"Plan": {"Node Type": "Seq Scan", "Relation Name": "t_a3f",
                                     "Total Cost": 19437.0, "Plan Rows": 1200, "Plans": []}},
            "bottleneck": {"op": "Seq Scan", "reason": "Full scan of the largest table dominates the plan cost.",
                           "evidence": {"table": "t_a3f", "cost_share": 0.92}},
            "recommendations": [{"kind": kind, "action": action, "cost_before": 19437.0,
                                 "cost_after": 812.0, "reason": reason}]}


if __name__ == "__main__":
    import tempfile
    AUDIT_LOG_PATH = Path(tempfile.mkdtemp()) / "audit_log.jsonl"   # keep the real log clean
    REAL_NAMES = ["orders", "region_id", "order_date", "rahul", "@example.com"]
    check = lambda t: not any(n in t.lower() for n in REAL_NAMES)

    samples = [
        ("1 index", _sample("index", "CREATE INDEX ON t_a3f (c_91b, c_d42)", "Filter columns have no index.")),
        ("2 rewrite", _sample("rewrite", "SELECT ... FROM t_a3f JOIN ... ON c_91b", "Subquery re-reads the table per row.")),
        ("3 LEAKY", _sample("index", "CREATE INDEX ON t_a3f (c_91b)",
                            "Customer Rahul (rahul@example.com) has no index on orders.customer_id.")),
    ]
    for label, s in samples:
        out = explain(s, check)
        print(f"[{label}] source={out['source']} prompt_ok={out['prompt_leak_ok']} "
              f"reply_ok={out['reply_leak_ok']} {out['latency_ms']}ms\n    {out['text']}\n"
              f"    fallback_reason: {out['fallback_reason']}")
    print("\n--- audit_log.jsonl ---")
    print(AUDIT_LOG_PATH.read_text(encoding="utf-8"))
    print("--- the exact prompt of analysis 1 ---")
    print(get_audit_log()[0]["prompt"])
