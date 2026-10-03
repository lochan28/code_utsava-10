"""Generate the fake sales database (regions, customers, products, orders).

Run:  .venv/Scripts/python db/generate.py            (uses N_ORDERS below)
      .venv/Scripts/python db/generate.py 5000000    (override order count)

Same SEED -> exactly the same data every time, so the whole team sees the
same slow queries and the same costs.

The tables are created WITHOUT helpful indexes on purpose (only primary
keys). That is what makes our demo queries slow and gives the AI something
to fix.
"""
import io
import sys
import time

import numpy as np
import pandas as pd
import psycopg

# ============================================================
# TUNABLE SETTINGS - safe to change, then re-run this script
# ============================================================
N_ORDERS = 1_000_000   # Size of the big table. 1M = fast to build (~1 min);
                       # 5-10M = slower queries, more dramatic demo numbers.
N_CUSTOMERS = 50_000   # More customers = each customer has fewer orders.
N_PRODUCTS = 2_000     # More products = lookups by product are more selective.
SEED = 42              # Change only if you want a different random dataset.
YEARS_OF_HISTORY = 3   # Orders are spread over this many years up to END_DATE.
END_DATE = "2026-09-30"
CHUNK = 200_000        # Rows sent to Postgres per batch. Lower if RAM is tight.

# ============================================================
# FIXED SETTINGS - must match docker-compose.yml
# ============================================================
DSN = "postgresql://tuner:tuner@localhost:5432/sales"

REGIONS = [  # (name, country)
    ("North", "India"), ("South", "India"), ("East", "India"), ("West", "India"),
    ("Central", "India"), ("Northeast", "India"), ("Gulf", "UAE"),
    ("Europe", "Germany"), ("Americas", "USA"), ("APAC", "Singapore"),
]
FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Vikram", "Meera", "Arjun", "Sara",
         "Kabir", "Isha", "Nikhil", "Priya", "Rahul", "Neha", "Karan", "Tara"]
LAST = ["Sharma", "Reddy", "Iyer", "Patel", "Singh", "Nair", "Gupta", "Rao",
        "Das", "Menon", "Khan", "Joshi", "Kulkarni", "Bose", "Verma", "Pillai"]
CATEGORIES = ["Electronics", "Furniture", "Clothing", "Grocery", "Toys",
              "Books", "Sports", "Beauty"]
STATUSES = ["delivered", "shipped", "pending", "cancelled", "returned"]
STATUS_P = [0.70, 0.12, 0.08, 0.06, 0.04]
SEGMENTS = ["consumer", "corporate", "small_business"]

SCHEMA = """
DROP TABLE IF EXISTS orders, customers, products, regions CASCADE;
CREATE TABLE regions   (region_id int PRIMARY KEY, name text, country text);
CREATE TABLE customers (customer_id int PRIMARY KEY, name text, email text,
                        region_id int, segment text, signup_date date);
CREATE TABLE products  (product_id int PRIMARY KEY, name text, category text,
                        price numeric(10,2));
CREATE TABLE orders    (order_id bigint PRIMARY KEY, customer_id int, region_id int,
                        product_id int, order_date date, quantity int,
                        amount numeric(12,2), status text);
"""


def copy_df(cur, table, df):
    """Bulk-load a DataFrame with COPY (much faster than INSERT)."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    with cur.copy(f"COPY {table} ({', '.join(df.columns)}) FROM STDIN WITH (FORMAT csv)") as cp:
        while data := buf.read(1 << 20):
            cp.write(data)


def make_regions():
    return pd.DataFrame({
        "region_id": range(1, len(REGIONS) + 1),
        "name": [r[0] for r in REGIONS],
        "country": [r[1] for r in REGIONS],
    })


def make_customers(rng, end):
    ids = np.arange(1, N_CUSTOMERS + 1)
    first = rng.choice(FIRST, N_CUSTOMERS)
    last = rng.choice(LAST, N_CUSTOMERS)
    days_ago = rng.integers(0, 365 * (YEARS_OF_HISTORY + 2), N_CUSTOMERS)
    region_p = np.linspace(2, 0.5, len(REGIONS))
    region_p /= region_p.sum()
    return pd.DataFrame({
        "customer_id": ids,
        "name": pd.Series(first) + " " + pd.Series(last),
        "email": [f"{f.lower()}.{l.lower()}{i}@example.com" for f, l, i in zip(first, last, ids)],
        # Uneven regions: earlier regions in the list get more customers.
        "region_id": rng.choice(len(REGIONS), N_CUSTOMERS, p=region_p) + 1,
        "segment": rng.choice(SEGMENTS, N_CUSTOMERS, p=[0.7, 0.2, 0.1]),
        "signup_date": end - pd.to_timedelta(days_ago, unit="D"),
    })


def make_products(rng):
    ids = np.arange(1, N_PRODUCTS + 1)
    cat = rng.choice(CATEGORIES, N_PRODUCTS)
    return pd.DataFrame({
        "product_id": ids,
        "name": [f"{c} item {i}" for c, i in zip(cat, ids)],
        "category": cat,
        "price": np.round(rng.lognormal(6.5, 1.0, N_PRODUCTS), 2),  # most ~Rs.300-2000
    })


def popularity(rng, size):
    """Probabilities where a few items are very popular and most are not.
    Shuffled so the popular ones are not just ids 1, 2, 3..."""
    p = 1.0 / np.arange(1, size + 1) ** 0.8
    return rng.permutation(p / p.sum())


def make_orders(rng, start_id, n, prices, end, cust_p, prod_p, cust_region):
    cust = rng.choice(N_CUSTOMERS, n, p=cust_p) + 1
    prod = rng.choice(N_PRODUCTS, n, p=prod_p) + 1
    qty = rng.integers(1, 6, n)
    days_ago = rng.integers(0, 365 * YEARS_OF_HISTORY, n)
    return pd.DataFrame({
        "order_id": np.arange(start_id, start_id + n),
        "customer_id": cust,
        # Copied from the customer, like a real sales table that stores the
        # sales region on each order. Needed for the PS example index
        # (region_id, order_date).
        "region_id": cust_region[cust - 1],
        "product_id": prod,
        "order_date": end - pd.to_timedelta(days_ago, unit="D"),
        "quantity": qty,
        "amount": np.round(prices[prod - 1] * qty, 2),
        "status": rng.choice(STATUSES, n, p=STATUS_P),
    })


def main(n_orders):
    rng = np.random.default_rng(SEED)
    end = pd.Timestamp(END_DATE)
    t0 = time.time()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        copy_df(cur, "regions", make_regions())
        customers = make_customers(rng, end)
        copy_df(cur, "customers", customers)
        products = make_products(rng)
        copy_df(cur, "products", products)
        prices = products["price"].to_numpy()
        cust_p, prod_p = popularity(rng, N_CUSTOMERS), popularity(rng, N_PRODUCTS)
        cust_region = customers["region_id"].to_numpy()
        for start in range(1, n_orders + 1, CHUNK):
            n = min(CHUNK, n_orders - start + 1)
            copy_df(cur, "orders", make_orders(rng, start, n, prices, end, cust_p, prod_p, cust_region))
            print(f"  orders loaded: {start + n - 1:,}/{n_orders:,}", flush=True)
        conn.commit()
        conn.autocommit = True
        cur.execute("VACUUM ANALYZE")  # fresh statistics so EXPLAIN costs are accurate
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else N_ORDERS)
