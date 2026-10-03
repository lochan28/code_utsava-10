"""Plain-English explanation from the local LLM (Ollama, qwen2.5:7b).

Owner: Person 4.  STATUS: Step 3 - the real Ollama call (plain HTTP), warm-up and
status flag are built and tested against a fake local server.  Not yet tried
against a real Ollama.  If Ollama is missing or slow, the template is used.

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
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ============================================================
# TUNABLE SETTINGS
# ============================================================
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
                      # Which model to ask. Switch WITHOUT editing code, e.g. if the 7b
                      # is too slow:  setx OLLAMA_MODEL qwen2.5:3b  (then reopen the
                      # terminal). The model must already be pulled with `ollama pull`.
MAX_PLAN_NODES = 6    # Plan steps shown to the LLM (most expensive first).
                      # More = more context but a longer prompt, so slower replies.
MAX_RECOMMENDATIONS = 3   # Fixes shown to the LLM and used in the fallback text.
                          # The LLM only needs the best few.
TIMEOUT_S = 30        # Give up on a normal reply after this many seconds, then use the
                      # template. Lower = the demo recovers faster when Ollama hangs;
                      # too low and a slow-but-working 7b gets cut off.
WARMUP_TIMEOUT_S = 120    # Loading the model into the GPU the first time can be slow
                          # (reading ~5 GB from disk), so the warm-up may wait longer.
TEMPERATURE = 0.2     # 0 = same answer every time, higher = more varied wording.
                      # Low keeps explanations factual.
MAX_REPLY_TOKENS = 200    # Hard cap on reply length (3 short sentences need ~80).
                          # Lower = faster, but may cut a sentence off.
NUM_CTX = 2048        # Context window in tokens. Our prompt is ~250 tokens, so 2048 is
                      # plenty; a bigger window uses more GPU memory for nothing.
                      # Keep warm-up and real calls on the SAME value, or Ollama reloads.

# ============================================================
# FIXED SETTINGS
# ============================================================
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")   # base URL, no path
                          # Deliberately 127.0.0.1, NOT "localhost": on Windows "localhost"
                          # tries IPv6 first, Ollama only listens on IPv4, and every request
                          # then waits ~2 s for the IPv6 attempt to fail (measured).
CONNECT_TIMEOUT_S = 1     # "Is Ollama even running?" - fail fast if not. A working local
                          # connection takes milliseconds; a refused one takes ~2 s on
                          # Windows unless we stop waiting earlier (measured).
WARMUP_PROMPT = "Reply with the single word: OK"
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
    """Ollama could not be used (not installed, not running, timed out, bad reply).
    `sent` says whether the prompt actually reached the server before it failed."""

    def __init__(self, message, sent=False):
        super().__init__(message)
        self.sent = sent


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
# Status: can we use the live LLM right now?
# ------------------------------------------------------------------
_status = {"status": "template fallback", "reason": "not checked yet"}
_warmup_started = False


def _set_status(status, reason=""):
    with _lock:
        _status.update(status=status, reason=reason)


def llm_status():
    """{"status": "live LLM" | "template fallback" | "warming up", "reason": ..., "model": ...}
    Updated by the warm-up and by every real call, so it reflects the latest outcome."""
    with _lock:
        return {**_status, "model": MODEL}


def _reset_state():
    """For tests only: forget the status and allow a new warm-up."""
    global _warmup_started
    with _lock:
        _warmup_started = False
        _status.update(status="template fallback", reason="not checked yet")


# ------------------------------------------------------------------
# The LLM call: plain HTTP to Ollama
# ------------------------------------------------------------------
# SECURITY: whatever the model replies is UNTRUSTED text. It is only ever DISPLAYED.
# It is never executed, never put into SQL, and never used as the "Approve" script
# (that script comes from the RL recommendation, un-masked by the pipeline).
def _post(prompt, timeout_s):
    """One request to Ollama. Returns (reply_text, stats). Raises LLMUnavailable."""
    body = {"model": MODEL, "prompt": prompt, "stream": False, "keep_alive": -1,
            "options": {"temperature": TEMPERATURE, "num_predict": MAX_REPLY_TOKENS,
                        "num_ctx": NUM_CTX}}
    try:
        resp = requests.post(OLLAMA_URL.rstrip("/") + "/api/generate", json=body,
                             timeout=(CONNECT_TIMEOUT_S, timeout_s))
    except requests.exceptions.ReadTimeout:
        raise LLMUnavailable(f"no reply within {timeout_s}s", sent=True) from None
    except requests.exceptions.ConnectionError:
        raise LLMUnavailable(f"Ollama is not running at {OLLAMA_URL}") from None
    except requests.exceptions.RequestException as e:
        raise LLMUnavailable(f"request failed: {type(e).__name__}") from None

    if resp.status_code == 404:
        raise LLMUnavailable(f"model '{MODEL}' is not downloaded - run: ollama pull {MODEL}", sent=True)
    if resp.status_code != 200:
        raise LLMUnavailable(f"Ollama returned HTTP {resp.status_code}", sent=True)
    try:
        data = resp.json()
        text = data["response"]
        if not isinstance(text, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise LLMUnavailable("Ollama sent a reply that could not be read", sent=True) from None

    tokens, nanos = data.get("eval_count"), data.get("eval_duration")
    stats = {}
    if isinstance(tokens, int) and isinstance(nanos, (int, float)) and nanos > 0:
        stats = {"tokens": tokens, "tokens_per_s": round(tokens / (nanos / 1e9), 1)}
    return text, stats


def _call_ollama(prompt):
    """Ask the model to explain. Returns (reply_text, stats) or raises LLMUnavailable."""
    return _post(prompt, TIMEOUT_S)


_FENCE = re.compile(r"```.*?(?:```|$)", re.S)


def clean_reply(text):
    """Display-only tidy-up of the model's reply. Removes fenced code blocks (including
    an unclosed one): SQL the model wrote must never sit on the page looking like a
    script to run. An explanation has no need for a code block, so any language goes."""
    return re.sub(r"\n{3,}", "\n\n", _FENCE.sub("", text)).strip()


def start_warmup():
    """Load the model into the GPU in the background, so the first question is not slow
    and the UI does not freeze. Safe to call on every Streamlit rerun: only the first
    call does anything."""
    global _warmup_started
    with _lock:
        if _warmup_started:
            return
        _warmup_started = True
        _status.update(status="warming up", reason=f"loading {MODEL}")
    threading.Thread(target=_warmup, daemon=True, name="ollama-warmup").start()


def _warmup():
    t0, reply, error, sent = time.perf_counter(), None, None, False
    try:
        reply, _ = _post(WARMUP_PROMPT, WARMUP_TIMEOUT_S)
        sent = True
        _set_status("live LLM", f"{MODEL} loaded and ready")
    except LLMUnavailable as e:
        sent, error = e.sent, str(e)
        _set_status("template fallback", error)
    audit_event("warmup", prompt=WARMUP_PROMPT, sent=sent,   # fixed text, holds no data
                reply=None if reply is None else reply[:80], error=error,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1))


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


def audit_event(event, **fields):
    """Record something other than an LLM call (e.g. 'analysis_blocked').
    Callers must pass only safe fields: ids, counts, hashes - never raw or leaked text."""
    _audit({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event, **fields})


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
      stats           {"tokens", "tokens_per_s"} from Ollama when it reports them, else {}
    """
    t0 = time.perf_counter()
    prompt = build_prompt(masked_result)
    prompt_ok = _is_clean(leak_check, prompt)

    reply, reply_ok, reason, sent, stats = None, None, None, False, {}
    if not prompt_ok:
        reason = "prompt failed the leak check, nothing was sent"
    else:
        try:
            reply, stats = _call_ollama(prompt)
            sent = True
            _set_status("live LLM", f"{MODEL} answered")
        except LLMUnavailable as e:
            sent = e.sent
            reason = f"LLM unavailable: {e}"
            _set_status("template fallback", str(e))

    text, source = None, "template"
    if reply is not None:
        reply_ok = _is_clean(leak_check, reply)       # check the raw reply, then tidy it
        shown = clean_reply(reply) if reply_ok else ""
        if reply_ok and shown:
            text, source = shown, "ollama"
        else:
            reason = ("reply failed the leak check" if not reply_ok
                      else "reply was empty after removing code blocks")

    if text is None:
        text = template_explanation(masked_result)
        if not _is_clean(leak_check, text):          # the facts themselves carry a leak
            text = SAFE_TEXT
            reason = (reason or "") + "; template text also failed the check"

    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    _audit({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": "llm_call",
        "source": source,
        "model": MODEL,
        "sent": sent,
        "prompt": prompt if prompt_ok else _redact(prompt),
        "prompt_leak_ok": prompt_ok,
        "reply": None if reply is None else (reply if reply_ok else _redact(reply)),
        "reply_leak_ok": reply_ok,
        "stats": stats,
        "latency_ms": latency_ms,
        "fallback_reason": reason,
    })
    return {"text": text, "source": source, "prompt_leak_ok": prompt_ok,
            "reply_leak_ok": reply_ok, "latency_ms": latency_ms, "fallback_reason": reason,
            "stats": stats}


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
