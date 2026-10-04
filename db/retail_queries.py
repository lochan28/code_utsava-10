"""Slow queries for the REAL retail database (UCI Online Retail II).

Owner: Person 1.  Same format as db/queries.py, so the same tools apply.
Values are real ones from the data (customer 15039, the shop's best seller
85123A "WHITE HANGING HEART T-LIGHT HOLDER", Germany = country 16).

Run:  .venv/Scripts/python -m db.retail_queries
      -> each query's real time, then with its expected fix (same answers checked).
"""
from db import sandbox

QUERIES = [
    {
        "id": "r01",
        "name": "Customer purchase history",
        "sql": "SELECT i.invoice_no, i.invoice_date, sum(l.quantity * l.price) AS total "
               "FROM invoices i JOIN invoice_lines l ON l.invoice_no = i.invoice_no "
               "WHERE i.customer_id = 15039 GROUP BY i.invoice_no, i.invoice_date ORDER BY i.invoice_date DESC",
        "problem": "Joins 1M invoice lines with no index on invoice_lines.invoice_no.",
        "fix_hint": ["CREATE INDEX ON invoice_lines (invoice_no)", "CREATE INDEX ON invoices (customer_id)"],
    },
    {
        "id": "r02",
        "name": "Monthly sales of the best seller",
        "sql": "SELECT date_trunc('month', i.invoice_date) AS month, sum(l.quantity) AS units "
               "FROM invoice_lines l JOIN invoices i ON i.invoice_no = l.invoice_no "
               "WHERE l.stock_code = '85123A' GROUP BY 1 ORDER BY 1",
        "problem": "Scans all 1M lines to find one product.",
        "fix_hint": ["CREATE INDEX ON invoice_lines (stock_code)"],
    },
    {
        "id": "r03",
        "name": "Top 10 products, Christmas 2010",
        "sql": "SELECT p.description, sum(l.quantity) AS units FROM invoice_lines l "
               "JOIN invoices i ON i.invoice_no = l.invoice_no "
               "JOIN products p ON p.stock_code = l.stock_code "
               "WHERE i.invoice_date >= '2010-12-01' AND i.invoice_date < '2011-01-01' "
               "AND NOT i.is_cancelled "
               "GROUP BY p.description ORDER BY units DESC, p.description LIMIT 10",
        "problem": "One month needed, but every invoice line is read and joined.",
        "fix_hint": ["CREATE INDEX ON invoices (invoice_date)", "CREATE INDEX ON invoice_lines (invoice_no)"],
    },
    {
        "id": "r04",
        "name": "Revenue by country, 2011",
        "sql": "SELECT n.name, sum(l.quantity * l.price) AS revenue FROM invoice_lines l "
               "JOIN invoices i ON i.invoice_no = l.invoice_no "
               "JOIN countries n ON n.country_id = i.country_id "
               "WHERE i.invoice_date >= '2011-01-01' GROUP BY n.name ORDER BY revenue DESC",
        "problem": "Big aggregate over most of the data - indexes cannot help much.",
        "fix_hint": [],
    },
    {
        "id": "r05",
        "name": "Last purchase of each German customer",
        "sql": "SELECT c.customer_id, (SELECT max(i.invoice_date) FROM invoices i "
               "WHERE i.customer_id = c.customer_id) AS last_purchase "
               "FROM customers c WHERE c.country_id = 16",
        "problem": "Subquery re-reads the invoices table once per customer.",
        "fix_hint": ["CREATE INDEX ON invoices (customer_id)"],
    },
]


def get(query_id):
    return next(q for q in QUERIES if q["id"] == query_id)


if __name__ == "__main__":
    sandbox.use_database("retail")
    print(f"{'id':4} {'name':38} {'before':>9} {'after':>9} {'gain':>6}  same answers  fix")
    for q in QUERIES:
        before = sandbox.checked_run(q["sql"])
        if q["fix_hint"]:
            after = sandbox.checked_run(q["sql"], q["fix_hint"])
            same = (after["rows"], after["checksum"]) == (before["rows"], before["checksum"])
            print(f"{q['id']:4} {q['name']:38} {before['ms']:7.1f}ms {after['ms']:7.1f}ms "
                  f"{100 * (1 - after['ms'] / before['ms']):5.0f}%  {str(same):12}  "
                  f"{' + '.join(i[16:] for i in q['fix_hint'])}")
        else:
            print(f"{q['id']:4} {q['name']:38} {before['ms']:7.1f}ms {'-':>9} {'-':>6}  {'-':12}  none expected")
