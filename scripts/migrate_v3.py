"""
migrate_v3.py — Adds v3 columns to an existing v2 signals.db.

Run once on the server after deploying v3:
    python scripts/migrate_v3.py
Safe to re-run (skips columns that already exist).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from src.database.db_logger import engine, init_db

NEW_COLUMNS = [
    ("strategy",        "VARCHAR"),
    ("regime",          "VARCHAR"),
    ("btc_regime",      "VARCHAR"),
    ("status",          "VARCHAR DEFAULT 'PENDING'"),
    ("fill_price",      "FLOAT"),
    ("filled_at",       "DATETIME"),
    ("r_multiple",      "FLOAT"),
    ("mfe_pct",         "FLOAT"),
    ("mae_pct",         "FLOAT"),
    ("last_checked_at", "DATETIME"),
    ("ml_win_prob",     "FLOAT"),
]


def migrate():
    init_db()
    with engine.connect() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(signals)"))}
        for col, coltype in NEW_COLUMNS:
            if col in existing:
                print(f"  skip {col} (exists)")
                continue
            conn.execute(text(f"ALTER TABLE signals ADD COLUMN {col} {coltype}"))
            print(f"  added {col}")
        # v2 open signals have no fill data — mark them ACTIVE with fill=signal price
        conn.execute(text(
            "UPDATE signals SET status='ACTIVE', fill_price=price_at_signal "
            "WHERE outcome IS NULL AND (status IS NULL OR status='PENDING') "
            "AND created_at < datetime('now', '-1 hour')"))
        conn.execute(text(
            "UPDATE signals SET status='CLOSED' WHERE outcome IS NOT NULL"))
        conn.commit()
    print("✅ Migration v3 complete.")


if __name__ == "__main__":
    migrate()
