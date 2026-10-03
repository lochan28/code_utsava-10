"""Ingestion of historical execution plans via Postgres's auto_explain.

Owner: Person 1.
auto_explain makes Postgres write the full plan of every slow statement to
its log. Our container logs to stderr, so plans are read back with
`docker logs`. Together with db.sandbox.slow_queries() (pg_stat_statements)
this is the "ingest slow query logs AND historical execution plans" part.

Plans contain real SQL text and values: the pipeline must mask them (Person 2)
before anything reaches ai/ or the LLM.

Self-test:  .venv/Scripts/python -m db.ingest
"""
import json
import re
import shutil
import subprocess

import psycopg

# ============================================================
# TUNABLE SETTINGS
# ============================================================
LOG_MIN_MS = 20          # a statement slower than this gets its plan logged.
                         # Lower = more plans (and more log noise); our slow
                         # queries take ~15-2500 ms on 1M orders. 0 = log all.
LOG_ACTUAL_ROWS = True   # also log ACTUAL row counts (EXPLAIN ANALYZE style), so
                         # estimated-vs-actual mistakes are visible to the GNN.
                         # Costs a little speed on every query while enabled.
LOG_LOOKBACK = "24h"     # how far back recent_plans() reads the container log.

# ============================================================
# FIXED SETTINGS - must match docker-compose.yml
# ============================================================
DSN = "postgresql://tuner:tuner@localhost:5432/sales"
CONTAINER = "utsav_pg"
DATABASE = "sales"

# One auto_explain entry starts like:  ... LOG:  duration: 23.456 ms  plan:
_ENTRY_START = re.compile(r"LOG:\s+duration: ([\d.]+) ms\s+plan:\s*$")


def enable_auto_explain():
    """Turn auto_explain on for the whole sales database (persistent setting,
    applies to NEW connections only - running sessions are not disturbed)."""
    settings = {
        "session_preload_libraries": "auto_explain",
        "auto_explain.log_min_duration": f"{LOG_MIN_MS}ms",
        "auto_explain.log_format": "json",
        "auto_explain.log_analyze": "on" if LOG_ACTUAL_ROWS else "off",
        "auto_explain.log_timing": "off",          # actual rows yes, per-node timing no (cheaper)
        "auto_explain.log_nested_statements": "off",
    }
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name, value in settings.items():
            conn.execute(f"ALTER DATABASE {DATABASE} SET {name} = '{value}'")


def disable_auto_explain():
    """Undo enable_auto_explain() (new connections stop logging plans)."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name in ["session_preload_libraries", "auto_explain.log_min_duration",
                     "auto_explain.log_format", "auto_explain.log_analyze",
                     "auto_explain.log_timing", "auto_explain.log_nested_statements"]:
            conn.execute(f"ALTER DATABASE {DATABASE} RESET {name}")


def _docker():
    found = shutil.which("docker")
    return found or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def _is_tooling(query_text):
    """Our own machinery, not the shop's real traffic."""
    q = query_text.lstrip()
    return (q.upper().startswith(("EXPLAIN", "/* SANDBOX */", "ANALYZE", "CREATE", "INSERT",
                                  "COPY", "VACUUM", "SET"))
            or "md5(t::text)" in q)


def recent_plans(limit=20):
    """Latest logged plans of slow statements, newest first:
    [{"duration_ms", "query", "plan"}] where plan is the EXPLAIN JSON
    ({"Plan": {...}}, with "Actual Rows" when LOG_ACTUAL_ROWS is on)."""
    log = subprocess.run([_docker(), "logs", "--since", LOG_LOOKBACK, CONTAINER],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = (log.stdout + log.stderr).splitlines()   # postgres logs to stderr
    entries, i = [], 0
    while i < len(lines):
        start = _ENTRY_START.search(lines[i])
        i += 1
        if not start:
            continue
        # The JSON follows on the next lines; collect until it parses.
        buf = ""
        while i < len(lines):
            buf += lines[i] + "\n"
            i += 1
            if buf.rstrip().endswith("}"):
                try:
                    doc = json.loads(buf)
                    break
                except json.JSONDecodeError:
                    continue
        else:
            break
        query = doc.get("Query Text", "")
        if not _is_tooling(query):
            entries.append({"duration_ms": float(start.group(1)), "query": query,
                            "plan": {"Plan": doc["Plan"]}})
    return entries[::-1][:limit]


if __name__ == "__main__":
    for e in recent_plans(5):
        print(f"{e['duration_ms']:8.1f} ms  {e['plan']['Plan']['Node Type']:18} {e['query'][:70]}")
