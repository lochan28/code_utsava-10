"""Obfuscation: hide table/column names and literal values before the AI sees anything.

Owner: Person 2.  STATUS: all functions real; self-test proves 11/11 queries masked.
Keep the signatures and return shapes (see CLAUDE.md "Shared interface contract").

How names are hidden: each table/column name is run through HMAC-SHA256 with
a SECRET KEY (privacy/.secret_key, never committed). Same key -> same codes
every run. Without the key nobody can rebuild the codes by hashing "orders".

Self-test:  .venv/Scripts/python -m privacy.mask
"""
import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path

import psycopg
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

# ============================================================
# TUNABLE SETTINGS
# ============================================================
HASH_LEN = 3   # Hex characters per code (t_7c2). 3 = short and readable for
               # the demo; if two names ever collide the code is lengthened
               # automatically. Raise to 6+ for a big real-world schema.

# ============================================================
# FIXED SETTINGS
# ============================================================
DSN = "postgresql://tuner:tuner@localhost:5432/sales"
SCHEMA = "public"
KEY_FILE = Path(__file__).with_name(".secret_key")   # changing/deleting it changes every code


class Mapping:
    """PRIVATE real-name <-> code table. Never pass this to ai/ or the LLM."""

    def __init__(self, tables, columns, schema):
        self.tables = tables          # {"orders": "t_7c2", ...}
        self.columns = columns        # {"region_id": "c_91b", ...}  (same name -> same code in every table)
        self.schema = schema          # {"orders": ["order_id", ...], ...}  real names, column order
        self.real_to_masked = {**tables, **columns}
        self.masked_to_real = {v: k for k, v in self.real_to_masked.items()}
        self.literals = set()         # every literal value masked so far (for the leak check)
        self.query_aliases = {}       # table aliases of the last masked query, reused by mask_plan


def _secret_key():
    """Load the secret key, creating a random one the first time."""
    if not KEY_FILE.exists():
        KEY_FILE.write_text(secrets.token_hex(32))
    return bytes.fromhex(KEY_FILE.read_text().strip())


def _codes(names, prefix, key):
    """Give every name a short keyed-hash code; lengthen codes until none collide."""
    length = HASH_LEN
    while True:
        codes = {n: prefix + hmac.new(key, f"{prefix}{n}".encode(), hashlib.sha256).hexdigest()[:length]
                 for n in names}
        if len(set(codes.values())) == len(codes):
            return codes
        length += 1


def build_mapping():
    """Read real tables and columns from the DB schema and code every name."""
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT c.table_name, c.column_name FROM information_schema.columns c "
            "JOIN information_schema.tables t USING (table_schema, table_name) "
            "WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE' "   # skips HypoPG / pg_stat views
            "ORDER BY c.table_name, c.ordinal_position", (SCHEMA,)).fetchall()
    schema = {}
    for table, column in rows:
        schema.setdefault(table, []).append(column)
    key = _secret_key()
    tables = _codes(sorted(schema), "t_", key)
    columns = _codes(sorted({c for cols in schema.values() for c in cols}), "c_", key)
    return Mapping(tables, columns, schema)


def mask_query(sql, mapping):
    """Rename every table/column/alias to its code and turn every literal into ?.

    Table aliases (o, c, r ...) become a1, a2 ... and output labels
    (AS revenue ...) become x1, x2 ...: short aliases hint at the real name.
    SQL keywords and functions (JOIN, sum, date_trunc) are kept - they carry
    no private data and the AI needs them.
    """
    tree = sqlglot.parse_one(sql, read="postgres")

    # 1) Give every alias in this query a neutral name, in order of appearance.
    aliases = {}
    for node in tree.find_all(exp.TableAlias, exp.Alias):
        is_table = isinstance(node, exp.TableAlias)
        name = (node.name if is_table else node.alias).lower()
        if name and name not in aliases:
            prefix = "a" if is_table else "x"
            aliases[name] = f"{prefix}{sum(v[0] == prefix for v in aliases.values()) + 1}"

    # 2) Rename identifiers according to where they appear.
    for ident in list(tree.find_all(exp.Identifier)):
        ident.set("this", _mask_identifier(ident, aliases, mapping))
        ident.set("quoted", False)

    # 3) Every literal value (numbers, strings, dates) becomes a placeholder.
    #    Exception: GROUP BY 1 / ORDER BY 2 mean "output column 1/2" - structure, not data.
    for lit in list(tree.find_all(exp.Literal)):
        if isinstance(lit.parent, (exp.Group, exp.Ordered)) and lit.is_int:
            continue
        mapping.literals.add(str(lit.this))
        lit.replace(exp.Placeholder())

    mapping.query_aliases = {k: v for k, v in aliases.items() if v.startswith("a")}
    return tree.sql(dialect="postgres").replace("%s", "?")


def _mask_identifier(ident, aliases, mapping):
    name, parent = ident.name.lower(), ident.parent
    if isinstance(parent, exp.Table) and ident.arg_key == "this":
        return mapping.tables.get(name) or _unknown(name, mapping)
    if isinstance(parent, (exp.TableAlias, exp.Alias)):
        return aliases[name]
    if isinstance(parent, exp.Column) and ident.arg_key == "table":   # the "o" in o.amount
        return aliases.get(name) or mapping.tables.get(name) or _unknown(name, mapping)
    if name in mapping.columns:
        return mapping.columns[name]
    if name in aliases:                                                # ORDER BY revenue
        return aliases[name]
    return _unknown(name, mapping)


def _unknown(name, mapping):
    """A name not in our schema still must not leak: give it a u_ code."""
    code = "u_" + hmac.new(_secret_key(), name.encode(), hashlib.sha256).hexdigest()[:HASH_LEN]
    mapping.real_to_masked[name] = code
    mapping.masked_to_real[code] = name
    return code


# Plan fields whose text is PostgreSQL vocabulary, never our data - kept as is.
PLAN_KEEP = {"Node Type", "Parent Relationship", "Join Type", "Strategy", "Partial Mode",
             "Scan Direction", "Sort Method", "Sort Space Type", "Subplan Name", "Schema",
             "Operation", "Async Capable", "Inner Unique", "Parallel Aware"}
# One token of a plan expression: 'quoted literal' | $1 | SubPlan 1 | word | number
TOKEN_RE = re.compile(r"'(?:[^']|'')*'|\$\d+|\b(?:SubPlan|InitPlan)\s+\d+|[A-Za-z_]\w*|\d+(?:\.\d+)?")


def mask_plan(plan, mapping):
    """Mask an EXPLAIN (FORMAT JSON) plan: names -> codes, values -> ?.

    Numbers about the plan itself (costs, row counts, widths) are kept - the
    GNN needs them and they describe the query, not the data.
    Call mask_query on the same SQL first: the plan then reuses its alias
    codes, so "a1" means the same table in the masked query and plan.
    """
    aliases = dict(mapping.query_aliases)
    for node in _plan_nodes(plan):
        alias, rel = node.get("Alias"), node.get("Relation Name")
        if alias and alias not in aliases:
            aliases[alias] = mapping.tables.get(alias) if alias == rel and alias in mapping.tables \
                else f"a{len(aliases) + 1}"
    return _mask_plan_value(plan, None, aliases, mapping)


def _plan_nodes(obj):
    if isinstance(obj, dict):
        if "Node Type" in obj:
            yield obj
        for v in obj.values():
            yield from _plan_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _plan_nodes(v)


def _mask_plan_value(value, key, aliases, mapping):
    if isinstance(value, dict):
        return {k: _mask_plan_value(v, k, aliases, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_plan_value(v, key, aliases, mapping) for v in value]
    if not isinstance(value, str) or key in PLAN_KEEP:
        return value
    if key == "Relation Name":
        return mapping.tables.get(value) or _unknown(value, mapping)
    if key == "Alias":
        return aliases[value]
    if key == "Index Name":                 # e.g. orders_pkey or HypoPG's <13625>btree_orders_region_id
        return "i_" + hmac.new(_secret_key(), value.encode(), hashlib.sha256).hexdigest()[:HASH_LEN]
    return TOKEN_RE.sub(lambda m: _mask_token(m.group(), aliases, mapping), value)


DATE_UNITS = {"microseconds", "milliseconds", "second", "minute", "hour", "day", "week",
              "month", "quarter", "year", "decade", "century", "millennium"}


def _mask_token(tok, aliases, mapping):
    if tok.startswith("'") and tok[1:-1].lower() in DATE_UNITS:
        return tok                          # date_trunc('month', ...) unit: SQL vocabulary, kept like in mask_query
    if tok.startswith("'"):                 # 'pending'  -> ?
        mapping.literals.add(tok[1:-1].replace("''", "'"))
        return "?"
    if tok[0].isdigit():                    # 3 -> ?
        mapping.literals.add(tok)
        return "?"
    if tok.startswith("$") or tok.startswith(("SubPlan", "InitPlan")):
        return tok                          # internal plan references, not data
    if tok in aliases:
        return aliases[tok]
    low = tok.lower()
    return mapping.tables.get(low) or mapping.columns.get(low) or tok   # keywords/functions stay


def column_bitmask(sql, mapping):
    """One bit per table column, in a fixed order: is it filtered / joined / grouped?

    Returns {"filter": "0010...", "join": ..., "group": ..., "columns": [...]};
    "columns" is the legend - the masked name of each bit position
    ("t_438.c_13b"), so the AI can read the bits without any real name.
    """
    order = [(t, c) for t in sorted(mapping.schema) for c in mapping.schema[t]]
    tree = qualify(sqlglot.parse_one(sql, read="postgres"), dialect="postgres",
                   schema={t: {c: "text" for c in cols} for t, cols in mapping.schema.items()})
    tables = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    real = lambda col: (tables.get(col.table), col.name)

    join, filt, group = set(), set(), set()
    join_uses = set()                          # ids of the column USES that are joins
    for eq in tree.find_all(exp.EQ):           # a.x = b.y between two tables -> join
        l, r = eq.left, eq.right
        if isinstance(l, exp.Column) and isinstance(r, exp.Column) and real(l)[0] != real(r)[0]:
            join |= {real(l), real(r)}
            join_uses |= {id(l), id(r)}
    for in_ in tree.find_all(exp.In):          # x IN (SELECT y ...) -> semi/anti join
        if in_.args.get("query"):
            cols = list(in_.this.find_all(exp.Column))
            sub = in_.args["query"].this
            cols += [c for e in sub.expressions for c in e.find_all(exp.Column)]
            join |= {real(c) for c in cols}
            join_uses |= {id(c) for c in cols}
    # A column can be both joined and filtered (q11: region_id) - decide per use.
    for where in tree.find_all(exp.Where, exp.Having):
        for col in where.find_all(exp.Column):
            if col.find_ancestor(exp.Select) is where.parent and id(col) not in join_uses:
                filt.add(real(col))
    for g in tree.find_all(exp.Group):
        group |= {real(c) for c in g.find_all(exp.Column)}

    bits = lambda s: "".join("1" if tc in s else "0" for tc in order)
    return {"filter": bits(filt), "join": bits(join), "group": bits(group),
            "columns": [f"{mapping.tables[t]}.{mapping.columns[c]}" for t, c in order]}


CODE_RE = re.compile(r"\b[tcu]_[0-9a-f]+\b")


def unmask(text, mapping):
    """Codes back to real names - ONLY for the admin's final screen."""
    return CODE_RE.sub(lambda m: mapping.masked_to_real.get(m.group(), m.group()), text)


def find_leaks(text, mapping, free_text=False):
    """List every real name or literal value still present in text.

    free_text=False (queries, plans, JSON): every real name counts as a leak.
    free_text=True (LLM explanations): plain-English column names like
    "name" or "status" are allowed, because they are normal words; table
    names, names with "_" and literal values still count.
    """
    try:                                   # JSON: check values only, not keys like "Relation Name"
        strings = list(_json_strings(json.loads(text)))
    except (ValueError, TypeError):
        strings = [text]
    names = set(mapping.tables) | {n for n in mapping.real_to_masked if n not in mapping.tables}
    if free_text:
        names = {n for n in names if n in mapping.tables or "_" in n}
    values = {v for v in mapping.literals if not free_text or not v.isdigit() or len(v) >= 3}
    leaks = set()
    for s in strings:
        for word in names | values:
            if re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", s, re.IGNORECASE):
                leaks.add(word)
    return sorted(leaks)


def verify_no_leak(text, mapping, free_text=False):
    """True if no real name or literal value remains. See find_leaks."""
    return not find_leaks(text, mapping, free_text)


def _json_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _json_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_strings(v)


def _self_test():
    """Proof for "100% masked": every query, plan and bitmask leak-free, and the
    checker catches every raw query and plan (so it is not just saying True)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db import queries

    m = build_mapping()
    print(f"Mapping: {len(m.tables)} tables, {len(m.columns)} column names -> codes\n")
    cols = ["query", "plan", "plan+verbose", "bitmask", "valid SQL", "raw query caught", "raw plan caught"]
    print(f"{'id':4} " + " ".join(f"{c:>16}" for c in cols))
    passed = 0
    with psycopg.connect(DSN) as conn:
        for q in queries.QUERIES:
            explain = lambda opts: conn.execute(f"EXPLAIN ({opts}) " + q["sql"]).fetchone()[0][0]
            plan, vplan = explain("FORMAT JSON"), explain("VERBOSE, FORMAT JSON")
            mq = mask_query(q["sql"], m)
            try:
                sqlglot.parse_one(mq.replace("?", "NULL"), read="postgres")
                valid = True
            except sqlglot.errors.ParseError:
                valid = False
            checks = [verify_no_leak(mq, m),
                      verify_no_leak(json.dumps(mask_plan(plan, m)), m),
                      verify_no_leak(json.dumps(mask_plan(vplan, m)), m),
                      verify_no_leak(json.dumps(column_bitmask(q["sql"], m)), m),
                      valid,
                      not verify_no_leak(q["sql"], m),
                      not verify_no_leak(json.dumps(plan), m)]
            passed += all(checks)
            print(f"{q['id']:4} " + " ".join(f"{'PASS' if c else 'FAIL':>16}" for c in checks))
    n = len(queries.QUERIES)
    print(f"\n{passed}/{n} queries fully masked; {len(m.literals)} literal values hidden.")
    print("Example (q11):", mask_query(queries.get("q11")["sql"], m)[:120], "...")
    return passed == n


if __name__ == "__main__":
    raise SystemExit(0 if _self_test() else 1)
