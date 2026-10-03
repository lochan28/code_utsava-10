"""The deliberately slow queries our tool will analyse and fix.

Each query is a question a shop manager might really ask. Each one is slow
for ONE clear reason (written in "problem"), so it is easy to explain to the
judges what the AI found. "fix_hint" is the answer we expect the AI to reach;
it is used only to check our own results, never shown to the AI.

Run:  .venv/Scripts/python db/queries.py
      -> runs every query once and prints its planner cost and real time.
"""
import json
import time

import psycopg

# ============================================================
# TUNABLE SETTINGS
# ============================================================
TIMEOUT_S = 60   # A query running longer than this is stopped (safety net).

# ============================================================
# FIXED SETTINGS - must match docker-compose.yml
# ============================================================
DSN = "postgresql://tuner:tuner@localhost:5432/sales"

QUERIES = [
    {
        "id": "q01",
        "name": "Customer order history",
        "sql": "SELECT order_id, order_date, amount, status FROM orders "
               "WHERE customer_id = 34950 ORDER BY order_date DESC",
        "problem": "Reads all orders to find one customer's few rows (no index on customer_id).",
        "fix_hint": "CREATE INDEX ON orders (customer_id)",
    },
    {
        "id": "q02",
        "name": "Monthly sales report by region",
        "sql": "SELECT r.name AS region, count(*) AS orders, sum(o.amount) AS revenue "
               "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
               "JOIN regions r ON r.region_id = c.region_id "
               "WHERE o.order_date BETWEEN '2026-08-01' AND '2026-08-31' "
               "GROUP BY r.name ORDER BY revenue DESC",
        "problem": "Scans 3 years of orders to use one month (no index on order_date).",
        "fix_hint": "CREATE INDEX ON orders (order_date)",
    },
    {
        "id": "q03",
        "name": "Recent pending orders",
        "sql": "SELECT order_id, customer_id, amount FROM orders "
               "WHERE status = 'pending' AND order_date >= '2026-09-20'",
        "problem": "Two filters, no index covering either.",
        "fix_hint": "CREATE INDEX ON orders (status, order_date)",
    },
    {
        "id": "q04",
        "name": "Lifetime spend of corporate customers",
        "sql": "SELECT c.name, (SELECT sum(o.amount) FROM orders o "
               "WHERE o.customer_id = c.customer_id) AS lifetime_spend "
               "FROM customers c WHERE c.segment = 'corporate' AND c.customer_id <= 300",
        "problem": "Subquery re-reads the whole orders table once per customer.",
        "fix_hint": "Rewrite as JOIN + GROUP BY, or CREATE INDEX ON orders (customer_id)",
    },
    {
        "id": "q05",
        "name": "August orders (date wrapped in a function)",
        "sql": "SELECT count(*), sum(amount) FROM orders "
               "WHERE date_trunc('month', order_date) = '2026-08-01'",
        "problem": "Function on the column hides it from any index on order_date.",
        "fix_hint": "Rewrite to order_date >= '2026-08-01' AND order_date < '2026-09-01'",
    },
    {
        "id": "q06",
        "name": "Top 10 electronics last quarter",
        "sql": "SELECT p.name, sum(o.amount) AS revenue FROM orders o "
               "JOIN products p ON p.product_id = o.product_id "
               "WHERE p.category = 'Electronics' AND o.order_date >= '2026-07-01' "
               "GROUP BY p.name ORDER BY revenue DESC LIMIT 10",
        "problem": "Big join on orders with no index on order_date or product_id.",
        "fix_hint": "CREATE INDEX ON orders (order_date) or (product_id, order_date)",
    },
    {
        "id": "q07",
        "name": "Customers who never ordered",
        "sql": "SELECT customer_id, name, email FROM customers "
               "WHERE customer_id NOT IN (SELECT customer_id FROM orders)",
        "problem": "NOT IN builds the full list of 1M order customer_ids first.",
        "fix_hint": "Rewrite as NOT EXISTS (anti-join) + CREATE INDEX ON orders (customer_id)",
    },
    {
        "id": "q08",
        "name": "Monthly revenue for 2025",
        "sql": "SELECT date_trunc('month', order_date) AS month, sum(amount) "
               "FROM orders WHERE order_date >= '2025-01-01' AND order_date < '2026-01-01' "
               "GROUP BY 1 ORDER BY 1",
        "problem": "One year out of three, but the single big table must be read in full.",
        "fix_hint": "Partition orders by order_date (yearly/monthly ranges)",
    },
    {
        "id": "q09",
        "name": "Orders by a customer OR for a product",
        "sql": "SELECT order_id, customer_id, product_id FROM orders "
               "WHERE customer_id = 123 OR product_id = 456",
        "problem": "OR across two columns, neither indexed.",
        "fix_hint": "CREATE INDEX ON orders (customer_id) + CREATE INDEX ON orders (product_id)",
    },
    {
        "id": "q10",
        "name": "Buyers of one product",
        "sql": "SELECT count(DISTINCT customer_id) FROM orders "
               "WHERE product_id = 907 AND status = 'delivered'",
        "problem": "Looks up one product by scanning every order.",
        "fix_hint": "CREATE INDEX ON orders (product_id)",
    },
    {
        # The exact scenario from the problem statement - our main demo query.
        "id": "q11",
        "name": "Weekly sales report by region",
        "sql": "SELECT r.name AS region, date_trunc('week', o.order_date) AS week, "
               "p.category, count(DISTINCT o.customer_id) AS buyers, sum(o.amount) AS revenue "
               "FROM orders o JOIN regions r ON r.region_id = o.region_id "
               "JOIN products p ON p.product_id = o.product_id "
               "JOIN customers c ON c.customer_id = o.customer_id "
               "WHERE o.region_id = 3 AND o.order_date >= '2026-09-01' "
               "AND c.segment <> 'small_business' "
               "GROUP BY r.name, week, p.category ORDER BY week, revenue DESC",
        "problem": "Nested join driven by a full scan of the big unpartitioned orders table.",
        "fix_hint": "CREATE INDEX ON orders (region_id, order_date) + partition orders by month",
    },
]


def get(query_id):
    """Look up one query by id, e.g. get('q02')."""
    return next(q for q in QUERIES if q["id"] == query_id)


def run_all():
    """Run every query once with EXPLAIN ANALYZE and print cost + real time."""
    print(f"{'id':4} {'name':40} {'plan cost':>12} {'real ms':>9}  slowest step")
    with psycopg.connect(DSN) as conn:
        conn.execute(f"SET statement_timeout = '{TIMEOUT_S}s'")
        for q in QUERIES:
            t0 = time.time()
            plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + q["sql"]).fetchone()[0][0]
            ms = (time.time() - t0) * 1000
            root = plan["Plan"]
            print(f"{q['id']:4} {q['name'][:40]:40} {root['Total Cost']:>12,.0f} {ms:>9,.0f}  {slowest_step(root)}")


def slowest_step(node):
    """Name the plan step that spends the most time by itself (not counting children)."""
    best, best_t = None, -1.0
    stack = [node]
    while stack:
        n = stack.pop()
        kids = n.get("Plans", [])
        self_t = n["Actual Total Time"] * n["Actual Loops"] - sum(
            k["Actual Total Time"] * k["Actual Loops"] for k in kids)
        if self_t > best_t:
            best, best_t = n, self_t
        stack.extend(kids)
    where = best.get("Relation Name", "")
    return f"{best['Node Type']} {where}".strip()


if __name__ == "__main__":
    run_all()
