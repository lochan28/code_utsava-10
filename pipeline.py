"""End-to-end pipeline: question in -> masked analysis -> recommendation out.

Owner: Person 4.  Works today with the stub modules; gets real as each
module is filled in.  The privacy boundary is HERE: real SQL and data stay
on the left (db/, privacy/), only masked data crosses to ai/ and app/llm.py.

Self-test:  .venv/Scripts/python pipeline.py
"""
import hashlib
import inspect
import json
import math
import os
import re

import sqlglot
from sqlglot import exp

from ai import gnn, rl
from app import llm
from db import queries, sandbox
from privacy import mask

# ============================================================
# TUNABLE SETTINGS
# ============================================================
MIN_CONFIDENCE = 0.35      # Below this, the question is "not understood" and we
                           # analyse the slowest query from the log instead.
                           # Raise it -> fewer wrong guesses but more fallbacks.
TIE_PENALTY = 0.5          # How much a close runner-up lowers confidence.
                           # 0 = ignore ties, 1 = a perfect tie gives 0% confidence.
DEFAULT_QUERY_ID = "q11"   # Last resort if the slow-query log is empty too.
PRIVACY_STRICT = os.environ.get("PRIVACY_STRICT", "1") != "0"
                           # ON (default): if masking leaves a real value behind, the
                           # analysis STOPS before the AI runs. Set PRIVACY_STRICT=0 to
                           # only warn (the event is still logged and the page shows
                           # "LEAK DETECTED"). The LLM prompt check stays on either way.
BLOCKED_MESSAGE = ("A value may not be masked, so the analysis was stopped for safety. "
                   "This was recorded in the audit log.")

# Extra words an admin might use for each known query (on top of its name).
KEYWORDS = {
    "q01": "customer order history past purchases",
    "q02": "monthly sales report region revenue",
    "q03": "recent pending orders open unshipped",
    "q04": "lifetime spend corporate customer total",
    "q05": "august orders month",
    "q06": "top electronics last quarter best selling products",
    "q07": "customers never ordered inactive no orders",
    "q08": "monthly revenue 2025 yearly annual",
    "q09": "orders customer or product either",
    "q10": "buyers of one product who bought",
    "q11": "weekly sales report region dashboard",
}
# Different words with the same meaning -> one word (applied after plural-stripping).
SYNONYMS = {
    "client": "customer", "user": "customer", "buyer": "customer",
    "purchase": "order", "sale": "sales", "earning": "revenue", "income": "revenue",
    "week": "weekly", "month": "monthly", "year": "yearly", "annual": "yearly",
    "bestselling": "top", "dashboard": "report",
}
# Words that carry no meaning for matching.
STOPWORDS = set("why is the a an of for my our are was so very slow slower taking "
                "long time timing out it this that what how show me get report's "
                "query queries down lagging lag hang hanging to in on by".split())


def _tokens(text):
    """Lowercase words, plurals stripped, synonyms merged, filler words removed."""
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(SYNONYMS.get(w, w))
    return out


# Each query's vocabulary = its name + its extra keywords.
_VOCAB = {q["id"]: set(_tokens(q["name"] + " " + KEYWORDS.get(q["id"], "")))
          for q in queries.QUERIES}
# Rare words (e.g. "weekly") say more than common ones (e.g. "order"): weight by IDF.
_IDF = {w: math.log(1 + len(_VOCAB) / sum(w in v for v in _VOCAB.values()))
        for v in _VOCAB.values() for w in v}


def _looks_like_sql(text):
    return re.match(r"\s*(select|with)\b", text, re.I) is not None


def _read_only_sql(text):
    """Accept exactly one read-only SELECT; reject anything that could change data."""
    try:
        stmts = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"Could not parse the pasted SQL: {e}") from None
    if len(stmts) != 1:
        raise ValueError("Paste exactly one SQL statement.")
    tree = stmts[0]
    if not isinstance(tree, exp.Query) or tree.find(
            exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
            exp.Into,   # SELECT ... INTO new_table  (creates a table)
            exp.Lock):  # SELECT ... FOR UPDATE      (takes row locks)
        raise ValueError("Only read-only SELECT queries can be analysed.")
    # sqlglot only turns functions it does not know into Anonymous. Those include
    # pg_sleep, pg_read_file, nextval, dblink ... so refuse them all.
    unknown = sorted({f.name for f in tree.find_all(exp.Anonymous)})
    if unknown:
        raise ValueError(f"Function(s) not allowed in pasted SQL: {', '.join(unknown)}")
    return tree.sql(dialect="postgres")


def _raw_literals(sql):
    """Real values inside raw SQL (text values, long numbers), lower-cased.

    Used ONLY as a tripwire: after masking, none of these may appear in anything
    that goes to the AI. It does not replace the masker, it double-checks it.
    Short values are skipped (a 1-2 character match would fire on ordinary words).
    """
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        return []
    found = set()
    for lit in (tree.find_all(exp.Literal) if tree else []):
        v = str(lit.this).lower()
        if len(v) >= (3 if lit.is_string else 5):
            found.add(v)
    return sorted(found)


class PrivacyError(ValueError):
    """Masked data failed the leak check. Raised BEFORE anything reaches the AI."""


def cache_key(match, masked_sql):
    """Key for caching a finished analysis (used in Step 5).

    Built ONLY from fixed ids and MASKED text. Never from the admin's question or
    pasted SQL, which can hold real names, emails or amounts.
    """
    if match["query_id"] == "adhoc":
        return "adhoc:" + hashlib.sha256(masked_sql.encode()).hexdigest()[:16]
    return "known:" + str(match["query_id"])


def match_question(question):
    """Decide which query to analyse.

    Returns (query, match) where query = {"id", "name", "sql"} and
    match = {"query_id", "name", "confidence" (0-1), "method", "runner_up"}.
    """
    if _looks_like_sql(question):
        sql = _read_only_sql(question)
        q = {"id": "adhoc", "name": "Pasted SQL (new, unseen query)", "sql": sql}
        return q, {"query_id": "adhoc", "name": q["name"], "confidence": 1.0,
                   "method": "raw SQL", "runner_up": None}

    words = [w for w in _tokens(question) if w in _IDF]   # ignore words no query knows
    total = sum(_IDF[w] for w in set(words))
    scores = sorted(((sum(_IDF[w] for w in set(words) if w in vocab), qid)
                     for qid, vocab in _VOCAB.items()), reverse=True)
    (best, best_id), (second, second_id) = scores[0], scores[1]
    confidence = 0.0
    if total > 0 and best > 0:
        coverage = best / total                  # share of the question we explained
        confidence = coverage * (1 - TIE_PENALTY * second / best)
    if confidence >= MIN_CONFIDENCE:
        q = queries.get(best_id)
        return q, {"query_id": q["id"], "name": q["name"], "confidence": round(confidence, 2),
                   "method": "keywords",
                   "runner_up": second_id if second > 0 else None}

    # Not understood: analyse the slowest query in the log instead of guessing.
    slow = sandbox.slow_queries(limit=1)
    if slow:
        known = {x["id"] for x in queries.QUERIES}
        s = slow[0]
        q = (queries.get(s["query_id"]) if s["query_id"] in known
             else {"id": str(s["query_id"]), "name": "Slowest query in the log", "sql": s["sql"]})
        method = "fallback: slowest query in log"
    else:
        q, method = queries.get(DEFAULT_QUERY_ID), "fallback: default query"
    return q, {"query_id": q["id"], "name": q["name"], "confidence": round(confidence, 2),
               "method": method, "runner_up": None}


# ============================================================
# Hooks that connect ai/ to the real database (Step 3.5)
# ============================================================
# Only actions of these two shapes are costed: one CREATE INDEX, or one monthly PARTITION
# (Person 1's sandbox can cost both). Anything else (YEARLY, several statements, DROP ...)
# is refused with ValueError, which P3 turns into "rule_only".
SIMULATABLE_ACTION = re.compile(
    r"\s*(CREATE\s+(UNIQUE\s+)?INDEX\b[^;]*"
    r"|PARTITION\s+\w+\s+BY\s+RANGE\s*\(\s*\w+\s*\)\s+MONTHLY)\s*;?\s*", re.I | re.S)


def make_cost_fn(original_sql, masked_original, mapping):
    """The AI's only window into the database for planner costs: masked text in, a number out."""
    def cost_fn(m_sql, m_actions):
        for action in m_actions:
            if not SIMULATABLE_ACTION.fullmatch(action):
                raise ValueError(f"cannot simulate this action (only CREATE INDEX): {action[:40]}")
        # The unchanged query is costed exactly as the admin wrote it; a REWRITE arrives
        # as new masked SQL and has to be un-masked first.
        sql = original_sql if m_sql == masked_original else mask.unmask(m_sql, mapping)
        return sandbox.cost(sql, [mask.unmask(a, mapping) for a in m_actions])
    return cost_fn


class TimeCheckError(RuntimeError):
    """The real timing run failed. Carries only the exception TYPE name: the original
    message could contain real names, and it must not reach the audit log."""


def make_time_fn(original_sql, masked_original, mapping, checks):
    """time_fn for ai/, or None until Person 1's sandbox has checked_run().

    The real run returns {"ms", "rows", "checksum"}. Rows and checksum are compared HERE
    against the original query and never leave this function: ai/ only gets
    {"ms", "results_match"}. Every call is also recorded in `checks` (a list)."""
    run = getattr(sandbox, "checked_run", None)
    if run is None:
        return None
    baseline = {}

    def time_fn(m_sql):
        try:
            sql = original_sql if m_sql == masked_original else mask.unmask(m_sql, mapping)
            if "run" not in baseline:
                baseline["run"] = run(original_sql)
            base = baseline["run"]
            res = base if sql == original_sql else run(sql)
            match = (res["rows"], res["checksum"]) == (base["rows"], base["checksum"])
            checks.append({"masked_sql": m_sql, "ms": res["ms"], "results_match": match})
            return {"ms": res["ms"], "results_match": match}
        except Exception as e:                           # noqa: BLE001
            raise TimeCheckError(type(e).__name__) from None
    return time_fn


def _complete_stats(stats):
    """Give P3 the keys it indexes directly. Numbers only, computed AFTER the leak check.
    Contract v2 supplies n_distinct; P3's code wants eq_sel = 1 / n_distinct."""
    out = dict(stats)
    out.setdefault("rows", {})
    out.setdefault("existing_indexes", [])
    out.setdefault("range_sel", {})
    if "eq_sel" not in out:
        eq = {}
        for key, nd in (stats.get("n_distinct") or {}).items():
            if isinstance(nd, (int, float)) and nd < 0:      # Postgres: -0.5 = half the rows
                nd = -nd * out["rows"].get(key.split(".")[0], 0)
            if isinstance(nd, (int, float)) and nd >= 1:
                eq[key] = 1.0 / nd
        out["eq_sel"] = eq
    return out


def _masked_extra(label, producer, is_clean, query_id):
    """Build ONE optional masked input for ai/ (stats, column_types).

    Never stops the analysis: if anything is wrong the input is skipped and an event
    goes to the audit log (ids, sizes, hashes, exception type - never the text itself).
    Returns (value or None, status text for the UI)."""
    if producer is None:
        return None, "not available yet"
    try:
        value = producer()
    except Exception as e:                               # noqa: BLE001
        llm.audit_event(f"{label}_unavailable", query_id=query_id, error=type(e).__name__)
        return None, "unavailable"
    blob = json.dumps(value, default=str)
    if not is_clean(blob):
        llm.audit_event(f"{label}_dropped", query_id=query_id, reason="failed the leak check",
                        chars=len(blob), sha256=hashlib.sha256(blob.encode()).hexdigest()[:12])
        return None, "dropped: failed the leak check"
    return value, "used"


def _accepted(fn, names):
    """Which of `names` can this function take as keyword arguments? (The stub
    ai/rl.py takes none; P3's real one takes all. Same pipeline works with both.)"""
    params = inspect.signature(fn).parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return set(names)
    return {n for n in names if n in params}


def analyze(question):
    # The raw question and any pasted SQL are UNTRUSTED and may hold real names,
    # emails or amounts. The raw question was used above for matching ONLY. From
    # here on, raw text is used for (a) the admin-only display and (b) the masker.
    # It must never reach a prompt, the audit log or a cache key - use masked_sql.
    q, match = match_question(question)
    mapping = mask.build_mapping()                      # private, stays here
    raw_literals = _raw_literals(q["sql"])

    def is_clean(text):
        """True if text holds no real name (masker's check) and none of the real
        values from the raw SQL (our own tripwire). Handed to llm.py so it never
        sees the mapping."""
        return mask.verify_no_leak(text, mapping) and not any(v in text.lower() for v in raw_literals)

    # TODO(Person 1, db/sandbox.py): q["sql"] may have been PASTED by the admin, so it
    # is untrusted. get_plan() and cost() must use plain EXPLAIN (never EXPLAIN ANALYZE)
    # with a statement_timeout, inside a read-only transaction. timed_run() is the only
    # function that really executes SQL: it must run only on the sandbox schema, in a
    # read-only transaction, with a statement_timeout. Also: the DB user "tuner" is a
    # SUPERUSER today; use a restricted read-only role for anything that runs pasted SQL.
    # ---- real side: only db/ and privacy/ see real SQL ----
    masked_sql = mask.mask_query(q["sql"], mapping)
    masked_plan = mask.mask_plan(sandbox.get_plan(q["sql"]), mapping)
    bitmask = mask.column_bitmask(q["sql"], mapping)

    # Gate: if masking left anything real behind, stop BEFORE the AI sees it.
    blob = json.dumps([masked_sql, masked_plan, bitmask])
    if not is_clean(blob):
        # Log ids, size and a hash only - never the leaked text itself.
        llm.audit_event("analysis_blocked" if PRIVACY_STRICT else "analysis_warned",
                        query_id=match["query_id"], strict=PRIVACY_STRICT,
                        reason="masked SQL/plan/bitmask failed the leak check",
                        chars=len(blob), sha256=hashlib.sha256(blob.encode()).hexdigest()[:12])
        if PRIVACY_STRICT:
            raise PrivacyError(BLOCKED_MESSAGE)

    cost_fn = make_cost_fn(q["sql"], masked_sql, mapping)

    # ---- AI side: masked data only ----
    bottleneck = gnn.find_bottleneck(masked_plan)

    # Optional extra inputs. Each one is built only if ai/ can take it AND the module that
    # supplies it exists yet; otherwise it is simply left out (no crash, no fake data).
    takes = _accepted(rl.recommend, ("time_fn", "stats", "column_types"))
    checks, extra, ai_inputs = [], {}, {}
    time_fn = make_time_fn(q["sql"], masked_sql, mapping, checks) if "time_fn" in takes else None
    if time_fn:
        extra["time_fn"] = time_fn
    ai_inputs["time_fn"] = ("used" if time_fn else "not available yet") if "time_fn" in takes \
        else "not supported by ai/ yet"
    for label, producer in (
            ("stats", (lambda: mask.mask_stats(sandbox.table_stats(q["sql"]), mapping))
             if hasattr(mask, "mask_stats") and hasattr(sandbox, "table_stats") else None),
            ("column_types", (lambda: mask.column_types(mapping))
             if hasattr(mask, "column_types") else None)):
        if label not in takes:
            ai_inputs[label] = "not supported by ai/ yet"
            continue
        value, ai_inputs[label] = _masked_extra(label, producer, is_clean, match["query_id"])
        if value is not None:
            extra[label] = _complete_stats(value) if label == "stats" else value

    try:
        recs = rl.recommend(masked_sql, masked_plan, cost_fn, **extra)
    except TimeCheckError as e:
        # The real timing run failed. P3's code does not catch that, so retry without it:
        # rewrites then show as unverified instead of the whole analysis dying.
        llm.audit_event("time_fn_failed", query_id=match["query_id"], error=str(e))
        extra.pop("time_fn", None)
        ai_inputs["time_fn"] = "failed: rewrites are unverified"
        recs = rl.recommend(masked_sql, masked_plan, cost_fn, **extra)

    # A rewrite whose rows differ from the original query's rows is UNSAFE. Flag it, and
    # sort it below every safe fix so it can never become the "best" one.
    unsafe = {c["masked_sql"] for c in checks if not c["results_match"]}
    for r in recs:
        if r.get("kind") == "rewrite" and r.get("action") in unsafe:
            r["unsafe_rewrite"] = True
            r["warning"] = "UNSAFE REWRITE: returns different rows than the original. Do not deploy."
    recs.sort(key=lambda r: bool(r.get("unsafe_rewrite")))     # stable: only moves unsafe ones down
    best = recs[0]
    masked_result = {"masked_query": masked_sql, "masked_plan": masked_plan,
                     "bitmask": bitmask, "bottleneck": bottleneck, "recommendations": recs}
    llm_out = llm.explain(masked_result, is_clean)
    explanation = llm_out["text"]

    # Everything the AI saw or produced must be free of real names/values.
    leak_check = is_clean(json.dumps(masked_result) + explanation)

    # ---- back on the real side: un-mask only for the admin's screen ----
    deploy_sql = mask.unmask(best["action"], mapping)
    t_before = sandbox.timed_run(q["sql"])
    t_after = sandbox.timed_run(q["sql"], [deploy_sql])
    return {
        "query_id": q["id"],
        "masked_query": masked_sql,
        "masked_plan": masked_plan,
        "bitmask": bitmask,
        "bottleneck": bottleneck,
        "recommendations": recs,
        "cost_before": best["cost_before"],
        "cost_after": best["cost_after"],
        "improvement_pct": round(100 * (1 - best["cost_after"] / best["cost_before"]), 1),
        "time_before_ms": t_before,
        "time_after_ms": t_after,
        "write_latency_ms": sandbox.measure_write_latency(deploy_sql)["delta_ms"],
        "storage_bytes": sandbox.index_size_bytes(deploy_sql),
        "explanation": explanation,
        "deploy_sql": deploy_sql,
        "leak_check": leak_check,
        # Extra key (not in the original contract; adding it breaks nobody):
        # how the question was matched, for the "matched X, 82% sure" line.
        "match": match,
        # Extra key: how the explanation was made + its leak-check results, for the UI.
        "llm": {k: llm_out[k] for k in ("source", "prompt_leak_ok", "reply_leak_ok",
                                        "latency_ms", "fallback_reason")},
        # Extra key: True if any rewrite returned different rows than the original query.
        "unsafe_rewrite": bool(unsafe),
        # Extra key: which optional inputs ai/ really received, e.g. {"time_fn": "used",
        # "stats": "dropped: failed the leak check", ...}. Lets the UI say why a rewrite
        # is "unverified" instead of leaving the admin guessing.
        "ai_inputs": ai_inputs,
    }


if __name__ == "__main__":
    result = analyze("Why is the weekly sales report slow?")
    for k, v in result.items():
        print(f"{k:16} {json.dumps(v)[:100]}")
