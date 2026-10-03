"""The Approve script: the text an admin may run BY HAND after reviewing it.

This file only builds text. It never connects to a database and never runs anything.
The script is the ONLY place where the un-masked SQL (real names, real values) is shown,
and the dashboard shows it only after the admin clicks Approve.

Self-test:  .venv/Scripts/python -m app.approve
"""
import re

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


def _body(kind, sql):
    """The lines between BEGIN and ROLLBACK."""
    statements = [p.strip() for p in sql.split(";") if p.strip()]
    if kind == "index" and statements and all(_CREATE_INDEX.match(s) for s in statements):
        return [s + ";" for s in statements]
    if kind == "rewrite" and sql:
        return ["-- A REPLACEMENT QUERY for your application. It reads data, it does not change it.",
                sql.strip().rstrip(";") + ";"]
    if kind == "partition" and sql:
        return ["-- Partitioning is a multi-step migration (create the partitioned table, copy the data,",
                "-- swap the names), not one statement, so no script is generated for it.",
                f"-- Recommended: {sql.strip()}",
                "-- Plan it with your DBA."]
    return ["-- No executable change was recommended."]


def build_script(kind, sql, label=""):
    """kind: 'index' | 'rewrite' | 'partition' | anything else. sql: the un-masked fix.
    label: how far to trust it, e.g. 'measured' (optional, shown in a comment)."""
    sql = (sql or "").strip()
    heading = f"-- Fix: {str(kind or 'unknown').upper()}" + (f" ({label})" if label else "")
    return "\n".join([*BANNER, heading, "BEGIN;", *_body(kind, sql), ROLLBACK_LINE]) + "\n"


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


if __name__ == "__main__":
    print(build_script("index", "CREATE INDEX ON orders (region_id, order_date)", "cost-simulated"))
    print(build_script("rewrite", "SELECT count(*) FROM orders WHERE customer_id = 5", "measured"))
