"""Obfuscation: hide table/column names and literal values before the AI sees anything.

Owner: Person 2.  STATUS: STUB - fake masking in the agreed shape.
Replace each function body; keep the signatures and return shapes
(see CLAUDE.md "Shared interface contract").

Self-test:  .venv/Scripts/python -m privacy.mask
"""


class Mapping:
    """PRIVATE real-name <-> hash table. Never pass this to ai/ or the LLM."""

    def __init__(self, real_to_masked):
        self.real_to_masked = real_to_masked
        self.masked_to_real = {v: k for k, v in real_to_masked.items()}


def build_mapping():
    return Mapping({"orders": "t_a3f", "region_id": "c_91b", "order_date": "c_d42"})


def mask_query(sql, mapping):
    return "SELECT ... FROM t_a3f WHERE t_a3f.c_91b = ? AND t_a3f.c_d42 >= ?"


def mask_plan(plan, mapping):
    return {"Plan": {"Node Type": "Seq Scan", "Relation Name": "t_a3f",
                     "Total Cost": 19437.0, "Plan Rows": 1200, "Plans": []}}


def column_bitmask(sql, mapping):
    return {"filter": "0110", "join": "1001", "group": "0001"}


def unmask(text, mapping):
    for masked, real in mapping.masked_to_real.items():
        text = text.replace(masked, real)
    return text


def verify_no_leak(text, mapping):
    return not any(real in text for real in mapping.real_to_masked)


if __name__ == "__main__":
    m = build_mapping()
    masked = mask_query("SELECT ...", m)
    print("masked:", masked)
    print("bitmask:", column_bitmask("SELECT ...", m))
    print("no leak:", verify_no_leak(masked, m))
    print("unmasked:", unmask("CREATE INDEX ON t_a3f (c_91b, c_d42)", m))
