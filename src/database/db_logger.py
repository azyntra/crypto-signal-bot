"""
db_logger.py — SQLAlchemy models for signal storage, deduplication, and P&L tracking.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Boolean, Text, func
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

from config.settings import DATABASE_URL
from config.logger import get_logger

logger = get_logger(__name__)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    __tablename__ = "signals"

    id           = Column(Integer, primary_key=True, index=True)
    symbol       = Column(String, nullable=False)
    exchange     = Column(String, nullable=False)
    market_type  = Column(String, nullable=False)       # spot / futures
    style        = Column(String, nullable=False)        # scalp / swing
    timeframe    = Column(String, nullable=False)
    direction    = Column(String, nullable=False)        # LONG / SHORT
    confidence   = Column(Float, nullable=False)

    entry_low    = Column(Float)
    entry_high   = Column(Float)
    tp1          = Column(Float)
    tp2          = Column(Float)
    tp3          = Column(Float)
    stop_loss    = Column(Float)
    rr_ratio     = Column(Float)

    price_at_signal  = Column(Float)
    price_at_close   = Column(Float, nullable=True)
    outcome          = Column(String, nullable=True)     # TP1 / TP2 / TP3 / SL / OPEN
    profit_pct       = Column(Float, nullable=True)

    reasons_json     = Column(Text)
    indicators_json  = Column(Text, nullable=True)

    sent_to_telegram = Column(Boolean, default=False)
    telegram_msg_id  = Column(Integer, nullable=True)

    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at    = Column(DateTime, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialised")


def save_signal(
    signal: dict,
    symbol: str,
    exchange: str,
    market_type: str,
    style: str,
    timeframe: str,
    telegram_msg_id: Optional[int] = None,
) -> SignalRecord:
    with SessionLocal() as db:
        record = SignalRecord(
            symbol           = symbol,
            exchange         = exchange,
            market_type      = market_type,
            style            = style,
            timeframe        = timeframe,
            direction        = signal["direction"],
            confidence       = signal["confidence"],
            entry_low        = signal.get("entry_low"),
            entry_high       = signal.get("entry_high"),
            tp1              = signal.get("tp1"),
            tp2              = signal.get("tp2"),
            tp3              = signal.get("tp3"),
            stop_loss        = signal.get("stop_loss"),
            rr_ratio         = signal.get("rr_ratio"),
            price_at_signal  = signal.get("price"),
            reasons_json     = json.dumps(signal.get("reasons", [])),
            sent_to_telegram = telegram_msg_id is not None,
            telegram_msg_id  = telegram_msg_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info(f"Signal saved: {symbol} {signal['direction']} id={record.id}")
        return record


def is_duplicate(symbol: str, exchange: str, direction: str, style: str, window_minutes: int = 60) -> bool:
    """Return True if an identical signal was sent within the last window_minutes."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    with SessionLocal() as db:
        count = db.query(func.count(SignalRecord.id)).filter(
            SignalRecord.symbol    == symbol,
            SignalRecord.exchange  == exchange,
            SignalRecord.direction == direction,
            SignalRecord.style     == style,
            SignalRecord.sent_to_telegram == True,
            SignalRecord.created_at >= cutoff,
        ).scalar()
        return (count or 0) > 0


def get_stats(days: int = 7) -> dict:
    """Return win/loss stats for the last N days."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with SessionLocal() as db:
        rows = db.query(SignalRecord).filter(
            SignalRecord.created_at >= cutoff,
            SignalRecord.outcome != None,
        ).all()

    total  = len(rows)
    wins   = sum(1 for r in rows if r.outcome in ("TP1", "TP2", "TP3"))
    losses = sum(1 for r in rows if r.outcome == "SL")
    wr     = wins / total * 100 if total else 0

    return {
        "total":  total,
        "wins":   wins,
        "losses": losses,
        "open":   sum(1 for r in rows if r.outcome == "OPEN"),
        "win_rate": round(wr, 1),
    }
