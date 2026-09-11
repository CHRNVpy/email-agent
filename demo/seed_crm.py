"""Create a demo CRM database (SQLite) with fictional customers, orders, deals and invoices.

    python -m demo.seed_crm                   # -> data/demo/crm.db
    python -m demo.seed_crm --path /tmp/crm.db

Dates are generated relative to today, so "this quarter" or "overdue" questions
always have data. The random seed is fixed: the same day gives the same data.
"""

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DEFAULT_PATH = Path("data/demo/crm.db")
DESCRIPTION = "CRM of Brightwave Labs: customers, orders, deals and invoices"
# Business definitions the schema alone cannot express (also in demo/.env.demo).
NOTES = (
    "Revenue means SUM(orders.amount) by orders.order_date; invoices are billing documents, not revenue. "
    "An invoice is overdue when status = 'open' and due_date is before today; days overdue = today - due_date."
)

sqlite3.register_adapter(date, date.isoformat)  # store dates as ISO strings

CUSTOMERS = [
    ("Acme Corp", "Manufacturing", "US"),
    ("Globex", "Energy", "US"),
    ("Initech", "Software", "US"),
    ("Umbrella Co", "Healthcare", "UK"),
    ("Bluepeak Retail", "Retail", "DE"),
    ("Cobalt Freight", "Logistics", "NL"),
    ("Dunmore Foods", "Food & Beverage", "IE"),
    ("Everline Health", "Healthcare", "US"),
    ("Fernbrook Energy", "Energy", "CA"),
    ("Granite Labs", "Biotech", "US"),
    ("Harbor & Pine", "Retail", "US"),
    ("Ironleaf Media", "Media", "UK"),
    ("Juniper Mobility", "Automotive", "DE"),
    ("Kestrel Aerospace", "Aerospace", "FR"),
    ("Lumen Grid", "Utilities", "ES"),
    ("Marlow Finance", "Financial Services", "UK"),
    ("Nimbus Travel", "Travel", "US"),
    ("Orchid Pharma", "Pharma", "CH"),
    ("Pinecrest Schools", "Education", "US"),
    ("Quarry Stone", "Construction", "AU"),
    ("Redwood Analytics", "Software", "US"),
    ("Saltmarsh Hotels", "Hospitality", "PT"),
    ("Tidewater Ports", "Logistics", "SG"),
    ("Upland Farms", "Agriculture", "NZ"),
]
PLANS = ["Starter", "Growth", "Growth", "Enterprise"]
MANAGERS = ["Anna Petrova", "Ben Ortiz", "Chloe Martin", "David Kim"]
STAGES = ["lead", "qualified", "proposal", "negotiation", "won", "lost"]
DEAL_TOPICS = ["Annual renewal", "Seat expansion", "Enterprise upgrade", "Snowflake connector", "Premium support"]

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, industry TEXT, country TEXT,
    plan TEXT, signup_date DATE, account_manager TEXT
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date DATE NOT NULL, amount NUMERIC NOT NULL
);
CREATE TABLE deals (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), title TEXT,
    stage TEXT NOT NULL, amount NUMERIC NOT NULL, created_at DATE, expected_close DATE
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
    issued_at DATE NOT NULL, due_date DATE NOT NULL, amount NUMERIC NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('paid', 'open'))
);
"""


def seed(path: Path, today: date | None = None) -> Path:
    today = today or date.today()
    rng = random.Random(7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for cid, (name, industry, country) in enumerate(CUSTOMERS, 1):
            signup = today - timedelta(days=rng.randint(60, 900))
            db.execute(
                "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, name, industry, country, rng.choice(PLANS), signup, rng.choice(MANAGERS)),
            )

            # Orders over the last ~13 months; a few customers went quiet 3+ months ago.
            size = rng.choice([2_000, 5_000, 9_000, 15_000, 30_000])
            quiet_since = rng.randint(95, 200) if cid % 6 == 0 else 0
            for _ in range(rng.randint(8, 22)):
                days_ago = rng.randint(quiet_since or 1, 400)
                db.execute(
                    "INSERT INTO orders (customer_id, order_date, amount) VALUES (?, ?, ?)",
                    (cid, today - timedelta(days=days_ago), round(size * rng.uniform(0.4, 1.6), 2)),
                )

            for _ in range(rng.randint(0, 3)):
                created = today - timedelta(days=rng.randint(5, 180))
                db.execute(
                    "INSERT INTO deals (customer_id, title, stage, amount, created_at, expected_close) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        rng.choice(DEAL_TOPICS),
                        rng.choice(STAGES),
                        round(size * rng.uniform(2, 8), -2),
                        created,
                        created + timedelta(days=rng.randint(30, 120)),
                    ),
                )

            for _ in range(rng.randint(3, 8)):
                issued = today - timedelta(days=rng.randint(1, 240))
                due = issued + timedelta(days=30)
                status = "paid" if due < today - timedelta(days=10) and rng.random() < 0.85 else "open"
                db.execute(
                    "INSERT INTO invoices (customer_id, issued_at, due_date, amount, status) VALUES (?, ?, ?, ?, ?)",
                    (cid, issued, due, round(size * rng.uniform(0.5, 1.5), 2), status),
                )
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    created = seed(parser.parse_args().path)
    with sqlite3.connect(created) as db:
        counts = {
            t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("customers", "orders", "deals", "invoices")
        }
    print(f"Demo CRM written to {created}: {counts}")
