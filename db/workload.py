"""Query-variant generator: many realistic slow queries for training and
testing the RL agent on questions it has never seen.

Owner: Person 1.
Each TEMPLATE is a question SHAPE (e.g. "one customer's orders in a date
range"); random values - and sometimes the column - are filled in from a seed.
The split is BY SHAPE: test templates never appear in training, so a good
test score means the agent handles new kinds of query, not just new values
in queries it already knows (requirement 5, "robustness").

Output is real SQL; mask it (privacy/, Person 2) before giving it to ai/.

Run:  .venv/Scripts/python -m db.workload                  (summary + examples)
      .venv/Scripts/python -m db.workload train 200 out.sql (one query per line)
"""
import random
import sys

# ============================================================
# TUNABLE SETTINGS
# ============================================================
SEED = 7                 # same seed -> same queries for everyone. Change for a fresh set.
DEFAULT_N = 100          # queries generated per split when no count is given.
TEST_TEMPLATES = ["t09", "t10", "t11", "t12"]   # shapes held out for testing. Move a
                         # template id here (or out) to make the test harder/easier.

# ============================================================
# FIXED SETTINGS - must match db/generate.py's data
# ============================================================
N_CUSTOMERS, N_PRODUCTS, N_REGIONS = 50_000, 2_000, 10
STATUSES = ["delivered", "shipped", "pending", "cancelled", "returned"]
SEGMENTS = ["consumer", "corporate", "small_business"]
CATEGORIES = ["Electronics", "Furniture", "Clothing", "Grocery", "Toys", "Books", "Sports", "Beauty"]
FIRST_DAY, DAYS = "2023-10-02", 1094       # orders span 2023-10-02 .. 2026-09-30


def _date(r, max_offset=DAYS):
    return f"DATE '{FIRST_DAY}' + {r.randint(0, max_offset)}"


def _window(r):
    """A random date window: start + a length of 7..120 days."""
    length = r.choice([7, 14, 30, 60, 90, 120])
    return _date(r, DAYS - length), length


def _lookup(r):
    """Return (column, random value) for a selective equality filter on orders."""
    col = r.choice(["customer_id", "product_id", "region_id", "status"])
    val = {"customer_id": r.randint(1, N_CUSTOMERS), "product_id": r.randint(1, N_PRODUCTS),
           "region_id": r.randint(1, N_REGIONS), "status": f"'{r.choice(STATUSES)}'"}[col]
    return col, val


# ---- TRAINING shapes -------------------------------------------------------
def t01(r):   # point lookup on one column
    col, val = _lookup(r)
    return f"SELECT order_id, order_date, amount FROM orders WHERE {col} = {val}"


def t02(r):   # date window aggregate
    start, n = _window(r)
    return (f"SELECT count(*), sum(amount) FROM orders "
            f"WHERE order_date >= {start} AND order_date < {start} + {n}")


def t03(r):   # two filters: column + date
    col, val = _lookup(r)
    start, n = _window(r)
    return (f"SELECT order_id, amount FROM orders WHERE {col} = {val} "
            f"AND order_date >= {start} AND order_date < {start} + {n}")


def t04(r):   # join customers, filter on segment + date, group by region
    start, n = _window(r)
    return (f"SELECT c.region_id, count(*), sum(o.amount) FROM orders o "
            f"JOIN customers c ON c.customer_id = o.customer_id "
            f"WHERE c.segment = '{r.choice(SEGMENTS)}' AND o.order_date >= {start} "
            f"AND o.order_date < {start} + {n} GROUP BY c.region_id")


def t05(r):   # join products, category + date, top N
    start, n = _window(r)
    return (f"SELECT p.product_id, sum(o.amount) AS revenue FROM orders o "
            f"JOIN products p ON p.product_id = o.product_id "
            f"WHERE p.category = '{r.choice(CATEGORIES)}' AND o.order_date >= {start} "
            f"AND o.order_date < {start} + {n} "
            f"GROUP BY p.product_id ORDER BY revenue DESC, p.product_id LIMIT {r.choice([5, 10, 20])}")


def t06(r):   # OR across two columns
    return (f"SELECT order_id FROM orders WHERE customer_id = {r.randint(1, N_CUSTOMERS)} "
            f"OR product_id = {r.randint(1, N_PRODUCTS)}")


def t07(r):   # function wrapped around the column (hides it from indexes)
    start, _ = _window(r)
    return (f"SELECT count(*) FROM orders "
            f"WHERE date_trunc('month', order_date) = date_trunc('month', {start})")


def t08(r):   # correlated subquery per customer
    lo = r.randint(1, N_CUSTOMERS - 200)
    return (f"SELECT c.customer_id, (SELECT max(o.order_date) FROM orders o "
            f"WHERE o.customer_id = c.customer_id) AS last_order FROM customers c "
            f"WHERE c.customer_id BETWEEN {lo} AND {lo + r.randint(20, 150)}")


# ---- TEST shapes (never in training) ---------------------------------------
def t09(r):   # IN-list of several customers
    ids = ", ".join(str(r.randint(1, N_CUSTOMERS)) for _ in range(r.randint(3, 8)))
    return f"SELECT customer_id, count(*), sum(amount) FROM orders WHERE customer_id IN ({ids}) GROUP BY customer_id"


def t10(r):   # three-way filter: region + status + date, weekly buckets
    start, n = _window(r)
    return (f"SELECT date_trunc('week', order_date) AS week, count(*) FROM orders "
            f"WHERE region_id = {r.randint(1, N_REGIONS)} AND status = '{r.choice(STATUSES)}' "
            f"AND order_date >= {start} AND order_date < {start} + {n} GROUP BY 1 ORDER BY 1")


def t11(r):   # NOT EXISTS: products not sold in a window
    start, n = _window(r)
    return (f"SELECT p.product_id, p.name FROM products p WHERE NOT EXISTS ("
            f"SELECT 1 FROM orders o WHERE o.product_id = p.product_id "
            f"AND o.order_date >= {start} AND o.order_date < {start} + {n})")


def t12(r):   # three-table join: customers + products + date, per category
    start, n = _window(r)
    return (f"SELECT p.category, count(DISTINCT o.customer_id) AS buyers FROM orders o "
            f"JOIN customers c ON c.customer_id = o.customer_id "
            f"JOIN products p ON p.product_id = o.product_id "
            f"WHERE c.region_id = {r.randint(1, N_REGIONS)} AND o.order_date >= {start} "
            f"AND o.order_date < {start} + {n} GROUP BY p.category")


TEMPLATES = {f.__name__: f for f in [t01, t02, t03, t04, t05, t06, t07, t08, t09, t10, t11, t12]}


def generate(n=DEFAULT_N, split="train", seed=SEED):
    """n queries from the train or test templates, spread evenly over them.
    Returns [{"id", "template", "split", "sql"}]. Same seed -> same list."""
    if split not in ("train", "test"):
        raise ValueError("split must be 'train' or 'test'")
    names = [t for t in TEMPLATES if (t in TEST_TEMPLATES) == (split == "test")]
    r = random.Random(f"{seed}-{split}")
    out = []
    for i in range(n):
        t = names[i % len(names)]
        out.append({"id": f"{split}_{i:04d}", "template": t, "split": split, "sql": TEMPLATES[t](r)})
    return out


def write_file(path, n=DEFAULT_N, split="train", seed=SEED):
    """One query per line, as Person 3 asked."""
    with open(path, "w", encoding="utf-8") as f:
        for q in generate(n, split, seed):
            f.write(q["sql"] + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 4:
        write_file(sys.argv[3], int(sys.argv[2]), sys.argv[1])
        print(f"wrote {sys.argv[2]} {sys.argv[1]} queries to {sys.argv[3]}")
    else:
        for split in ("train", "test"):
            qs = generate(split=split)
            print(f"{split}: {len(qs)} queries over templates {sorted({q['template'] for q in qs})}")
            for q in qs[:2]:
                print(f"   {q['template']}: {q['sql'][:100]}")
