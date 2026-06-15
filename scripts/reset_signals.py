"""
reset_signals.py — Clean up the signal database.
Expires all open signals and removes stablecoin signal history
so the adaptive system can start fresh with clean data.

Usage:
    PYTHONPATH=. python scripts/reset_signals.py
"""
from datetime import datetime, timezone
from src.database.db_logger import SessionLocal, SignalRecord, init_db

STABLECOIN_SYMBOLS = {
    "USDE/USDT", "USDTB/USDT", "USD1/USDT", "PYUSD/USDT", "BFUSD/USDT",
    "EURC/USDT", "GHO/USDT", "FRAX/USDT", "CRVUSD/USDT", "LUSD/USDT",
    "SUSD/USDT", "MIM/USDT", "GUSD/USDT", "USDD/USDT", "DOLA/USDT",
    "WBTC/USDT", "WETH/USDT", "STETH/USDT", "CBETH/USDT", "RETH/USDT",
    "WSTETH/USDT", "CBBTC/USDT", "WBETH/USDT",
}


def main():
    init_db()
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        # 1. Expire all open signals
        open_signals = db.query(SignalRecord).filter(
            SignalRecord.outcome.is_(None)
        ).all()
        print(f"Found {len(open_signals)} open signals — expiring all...")
        for rec in open_signals:
            rec.outcome = "EXPIRED"
            rec.profit_pct = 0.0
            rec.closed_at = now
        db.commit()
        print(f"  ✅ Expired {len(open_signals)} open signals.")

        # 2. Delete stablecoin signal records (poisoned data)
        stablecoin_count = db.query(SignalRecord).filter(
            SignalRecord.symbol.in_(STABLECOIN_SYMBOLS)
        ).count()
        if stablecoin_count > 0:
            db.query(SignalRecord).filter(
                SignalRecord.symbol.in_(STABLECOIN_SYMBOLS)
            ).delete(synchronize_session=False)
            db.commit()
            print(f"  ✅ Deleted {stablecoin_count} stablecoin signal records.")
        else:
            print("  ℹ️  No stablecoin signals found.")

        # 3. Summary
        total = db.query(SignalRecord).count()
        closed = db.query(SignalRecord).filter(
            SignalRecord.outcome.isnot(None)
        ).count()
        print(f"\n📊 Database summary: {total} total signals, {closed} closed, {total - closed} open.")

    print("\n✅ Database cleaned. Adaptive system will start fresh.")
    print("   Re-enable ADAPTIVE_CONFIDENCE=True in settings.py after 24-48h of clean data.")


if __name__ == "__main__":
    main()
