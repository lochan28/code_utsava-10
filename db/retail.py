"""Load the REAL UCI "Online Retail II" dataset into a separate database, `retail`.

Owner: Person 1.  Purpose: show the judges the same tool works on real data.
The team's `sales` database is never touched.

Source: UCI Machine Learning Repository, dataset 502 (UK online gift shop,
2009-12-01 .. 2011-12-09, ~1.07M sales lines). Download it once into
data/retail/ (git-ignored); the first run converts the Excel file to CSV.

Real data is messy, and we keep it that way on purpose:
  - guest checkouts (no customer id) -> invoices.customer_id is NULL
  - cancellations (invoice "C...", negative quantity) -> invoices.is_cancelled
  - only EXACT duplicate lines are removed

Run:  .venv/Scripts/python -m db.retail
"""
import io
import time
from pathlib import Path

import pandas as pd
import psycopg

# ============================================================
# TUNABLE SETTINGS
# ============================================================
DROP_EXACT_DUPLICATES = True   # True = remove lines identical in every column
                               # (~34k). False = keep the file exactly as published.

# ============================================================
# FIXED SETTINGS
# ============================================================
ADMIN_DSN = "postgresql://tuner:tuner@localhost:5432/sales"    # used only to CREATE DATABASE
DSN = "postgresql://tuner:tuner@localhost:5432/retail"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "retail"
XLSX, CSV = DATA_DIR / "online_retail_II.xlsx", DATA_DIR / "online_retail_II.csv"

SCHEMA = """
DROP TABLE IF EXISTS invoice_lines, invoices, products, customers, countries CASCADE;
CREATE TABLE countries     (country_id int PRIMARY KEY, name text);
CREATE TABLE customers     (customer_id int PRIMARY KEY, country_id int);
CREATE TABLE products      (stock_code text PRIMARY KEY, description text, unit_price numeric(10,2));
CREATE TABLE invoices      (invoice_no text PRIMARY KEY, customer_id int, country_id int,
                            invoice_date timestamp, is_cancelled boolean);
CREATE TABLE invoice_lines (line_id bigint PRIMARY KEY, invoice_no text, stock_code text,
                            quantity int, price numeric(10,2));
"""


def read_raw():
    """All sales lines as one DataFrame (Excel is read once, then cached as CSV)."""
    if not CSV.exists():
        sheets = pd.read_excel(XLSX, sheet_name=None)
        pd.concat(sheets.values(), ignore_index=True).to_csv(CSV, index=False)
    df = pd.read_csv(CSV, dtype={"Invoice": str, "StockCode": str}, parse_dates=["InvoiceDate"])
    if DROP_EXACT_DUPLICATES:
        df = df.drop_duplicates()
    return df.reset_index(drop=True)


def build_tables(df):
    """Split the flat file into a normal shop schema."""
    countries = pd.DataFrame({"name": sorted(df["Country"].unique())})
    countries["country_id"] = range(1, len(countries) + 1)
    cid = dict(zip(countries["name"], countries["country_id"]))

    known = df.dropna(subset=["Customer ID"])
    customers = (known.sort_values("InvoiceDate").groupby("Customer ID")["Country"].first()
                 .reset_index())                                      # country of first purchase
    customers = pd.DataFrame({"customer_id": customers["Customer ID"].astype(int),
                              "country_id": customers["Country"].map(cid)})

    products = (df.groupby("StockCode")
                .agg(description=("Description", lambda s: s.mode().iat[0] if s.notna().any() else None),
                     unit_price=("Price", "median")).reset_index()
                .rename(columns={"StockCode": "stock_code"}))
    products["unit_price"] = products["unit_price"].round(2)

    inv = df.sort_values("InvoiceDate").groupby("Invoice").agg(
        customer_id=("Customer ID", "first"), country=("Country", "first"),
        invoice_date=("InvoiceDate", "first")).reset_index()
    invoices = pd.DataFrame({
        "invoice_no": inv["Invoice"],
        "customer_id": inv["customer_id"].astype("Int64"),             # NULL for guests
        "country_id": inv["country"].map(cid),
        "invoice_date": inv["invoice_date"],
        "is_cancelled": inv["Invoice"].str.startswith("C")})

    lines = pd.DataFrame({"line_id": range(1, len(df) + 1), "invoice_no": df["Invoice"],
                          "stock_code": df["StockCode"], "quantity": df["Quantity"],
                          "price": df["Price"].round(2)})
    return {"countries": countries[["country_id", "name"]], "customers": customers,
            "products": products, "invoices": invoices, "invoice_lines": lines}


def ensure_database():
    """Create the `retail` database (with HypoPG + pg_stat_statements) if missing."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = 'retail'").fetchone():
            admin.execute("CREATE DATABASE retail")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS hypopg")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def load():
    t0 = time.time()
    tables = build_tables(read_raw())
    ensure_database()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        for name, frame in tables.items():
            buf = io.StringIO()
            frame.to_csv(buf, index=False, header=False)
            buf.seek(0)
            with cur.copy(f"COPY {name} ({', '.join(frame.columns)}) FROM STDIN WITH (FORMAT csv)") as cp:
                cp.write(buf.read())
            print(f"  {name:14} {len(frame):>9,} rows")
        conn.commit()
        conn.autocommit = True
        cur.execute("VACUUM ANALYZE")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    load()
