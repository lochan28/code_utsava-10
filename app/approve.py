"""The Approve script: the text an admin may run BY HAND after reviewing it.

This file only builds text. It never connects to a database and never runs anything.
The script is the ONLY place where the un-masked SQL (real names, real values) is shown,
and the dashboard shows it only after the admin clicks Approve.

Self-test:  .venv/Scripts/python -m app.approve
"""
import re

import sqlglot
from sqlglot import exp

# ============================================================
# FIXED TEXT (the safety wording; changing it changes what the admin is promised)
# ============================================================
BANNER = [
    "-- ============================================================",
    "-- NOT EXECUTED. Review before running.",
    "-- The AI Database Tuner has not changed anything in your database.",
    "-- Run as it stands, this script keeps NOTHING: it ends with ROLLBACK.",
    "-- ============================================================",
]
ROLLBACK_LINE = "ROLLBACK;  -- safe default: nothing is kept. Change to COMMIT; only after you have reviewed the result."

_CREATE_INDEX = re.compile(r"\s*CREATE\s+(UNIQUE\s+)?INDEX\b", re.I)


def _one_line(text):
    """Text that goes INSIDE a comment: one line, so nothing after a newline can escape the comment."""
    return " ".join(str(text).split())


def _is_index(statement):
    return bool(_CREATE_INDEX.match(statement)) and not any(c in statement for c in chr(10) + chr(13))


def _is_one_select(sql):
    """True only for exactly one read-only SELECT (so it is safe to show as a statement)."""
    try:
        stmts = [x for x in sqlglot.parse(sql, read="postgres") if x is not None]
    except sqlglot.errors.SqlglotError:
        return False
    return len(stmts) == 1 and isinstance(stmts[0], exp.Query) and not stmts[0].find(
        exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Into, exp.Lock)


def _body(kind, sql):
    """The lines between BEGIN and ROLLBACK."""
    statements = [p.strip() for p in sql.split(";") if p.strip()]
    if kind == "index" and statements and all(_is_index(s) for s in statements):
        return [s + ";" for s in statements]
    if kind == "rewrite" and sql and _is_one_select(sql):
        return ["-- A REPLACEMENT QUERY for your application. It reads data, it does not change it.",
                sql.strip().rstrip(";") + ";"]
    if kind == "rewrite" and sql:        # not one plain SELECT: shown, but only as comments
        return ["-- A suggested rewrite that is not a single SELECT, so it is shown as comments only:",
                *[f"--   {line}" for line in sql.strip().splitlines()]]
    if kind == "partition" and sql:
        partition = [s for s in statements if not _is_index(s)]
        indexes = [s + ";" for s in statements if _is_index(s)]    # a combined fix: PARTITION ...; CREATE INDEX ...
        lines = ["-- Partitioning is a multi-step migration (create the partitioned table, copy the data,",
                 "-- swap the names), not one statement, so no script is generated for it.",
                 f"-- Recommended: {_one_line('; '.join(partition))}",
                 "-- Plan it with your DBA."]
        if indexes:
            lines += ["-- The same fix also adds an index (a normal statement):", *indexes]
        return lines
    return ["-- No executable change was recommended."]


def _rewrite_lines(rewrite_sql, rule):
    """A query rewrite that was timed together with the fix: a code change for the APPLICATION.
    It is a SELECT, so it is written as comments: nothing in it can be run by this script."""
    rewrite_sql = (rewrite_sql or "").strip().rstrip(";")
    if not rewrite_sql:
        return []
    rule = _one_line(rule)[:60] if isinstance(rule, str) else ""
    return ["-- ALSO: replace the query in your application with this one" + (f" (rewrite rule: {rule})" if rule else ""),
            "-- (it only reads data; it is a code change, not something to run here):",
            *[f"--   {line}" for line in rewrite_sql.splitlines()]]


def build_script(kind, sql, label="", rewrite_sql="", rewrite_rule=""):
    """kind: 'index' | 'rewrite' | 'partition' | anything else. sql: the un-masked fix.
    label: how far to trust it, e.g. 'measured' (optional, shown in a comment).
    rewrite_sql / rewrite_rule: the query rewrite timed together with the fix (optional)."""
    sql = (sql or "").strip()
    heading = f"-- Fix: {_one_line(kind or 'unknown').upper()}" + (f" ({_one_line(label)})" if label else "")
    return "\n".join([*BANNER, heading, "BEGIN;", *_body(kind, sql), *_rewrite_lines(rewrite_sql, rewrite_rule),
                      ROLLBACK_LINE]) + "\n"


def sql_for(result, rec):
    """The un-masked SQL that belongs to `rec` (the fix the page calls the top pick), or None.
    The pipeline keeps result["deploy_scripts"] aligned with result["recommendations"]; an older
    result without it can only give the script of its FIRST recommendation (deploy_sql)."""
    recs = result.get("recommendations")
    if not isinstance(recs, list):
        return None
    index = next((i for i, r in enumerate(recs) if r is rec), None)
    scripts = result.get("deploy_scripts")
    if index is not None and isinstance(scripts, list) and index < len(scripts) and isinstance(scripts[index], str):
        return scripts[index]
    if index == 0 and isinstance(result.get("deploy_sql"), str):
        return result["deploy_sql"]
    return None


def rewrite_for(result, rec):
    """The un-masked query rewrite that was timed together with `rec`, or "" (none / not found).
    result["deploy_rewrites"] is aligned with result["recommendations"], like deploy_scripts."""
    recs = result.get("recommendations")
    rewrites = result.get("deploy_rewrites")
    if not isinstance(recs, list) or not isinstance(rewrites, list):
        return ""
    index = next((i for i, r in enumerate(recs) if r is rec), None)
    if index is not None and index < len(rewrites) and isinstance(rewrites[index], str):
        return rewrites[index]
    return ""


if __name__ == "__main__":
    print(build_script("index", "CREATE INDEX ON orders (region_id, order_date)", "cost-simulated"))
    print(build_script("rewrite", "SELECT count(*) FROM orders WHERE customer_id = 5", "measured"))
