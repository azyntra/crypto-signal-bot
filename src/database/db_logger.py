"""
db_logger.py — SQLAlchemy models (v3).

v3 lifecycle: PENDING (waiting for entry fill) → ACTIVE → closed
Outcomes: TP1/TP2/TP3 (highest hit), SL, BE (breakeven after trail),
          NOFILL (entry never reached), EXPIRED (timed out while active).
"""
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Boolean, Text, func,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config.settings import DATABASE_URL
from config.logger import get_logger

logger = get_logger(__name__)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

WIN_OUTCOMES  = ("TP1", "TP2", "TP3")
LOSS_OUTCOMES = ("SL",)


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    __tablename__ = "signals"

    id          = Column(Integer, primary_key=True, index=True)
    symbol      = Column(String, nullable=False)
    exchange    = Column(String, nullable=False)
    market_type = Column(String, nullable=False)
    style       = Column(String, nullable=False)      # intraday / swing
    timeframe   = Column(String, nullable=False)
    direction   = Column(String, nullable=False)      # LONG / SHORT
    strategy    = Column(String, nullable=True)       # trend_pullback / range_fade / squeeze_breakout
    regime      = Column(String, nullable=True)
    btc_regime  = Column(String, nullable=True)
    confidence  = Column(Float, nullable=False)

    entry_low   = Column(Float)
    entry_high  = Column(Float)
    tp1         = Column(Float)
    tp2         = Column(Float)
    tp3         = Column(Float)
    stop_loss   = Column(Float)
    rr_ratio    = Column(Float)

    # Lifecycle
    status          = Column(String, default="PENDING")   # PENDING / ACTIVE / CLOSED
    price_at_signal = Column(Float)
    fill_price      = Column(Float, nullable=True)
    filled_at       = Column(DateTime, nullable=True)
    price_at_close  = Column(Float, nullable=True)
    outcome         = Column(String, nullable=True)        # TP1/TP2/TP3/SL/BE/NOFILL/EXPIRED
    profit_pct      = Column(Float, nullable=True)
    r_multiple      = Column(Float, nullable=True)         # realized R (scaled-exit model)
    mfe_pct         = Column(Float, nullable=True)         # max favorable excursion
    mae_pct         = Column(Float, nullable=True)         # max adverse excursion

    highest_tp_hit  = Column(String, nullable=True)
    adjusted_sl     = Column(Float, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)

    reasons_json    = Column(Text)
    indicators_json = Column(Text, nullable=True)

    sent_to_telegram = Column(Boolean, default=False)
    telegram_msg_id  = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at  = Column(DateTime, nullable=True)

    ai_decision            = Column(String, nullable=True)
    ai_adjusted_confidence = Column(Integer, nullable=True)
    ai_reasoning           = Column(Text, nullable=True)
    ml_win_prob            = Column(Float, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialised")


def save_signal(
    signal: dict, symbol: str, exchange: str, market_type: str,
    style: str, timeframe: str, telegram_msg_id: Optional[int] = None,
    ai_review: Optional[dict] = None, regime: Optional[str] = None,
    btc_regime: Optional[str] = None,
) -> SignalRecord:
    # Keep indicators JSON small: strip non-serializable / bulky values
    ind = {k: v for k, v in (signal.get("indicators") or {}).items()
           if isinstance(v, (int, float, bool, str, type(None)))}
    with SessionLocal() as db:
        record = SignalRecord(
            symbol=symbol, exchange=exchange, market_type=market_type,
            style=style, timeframe=timeframe,
            direction=signal["direction"],
            strategy=signal.get("strategy"),
            regime=regime, btc_regime=btc_regime,
            confidence=signal["confidence"],
            entry_low=signal.get("entry_low"), entry_high=signal.get("entry_high"),
            tp1=signal.get("tp1"), tp2=signal.get("tp2"), tp3=signal.get("tp3"),
            stop_loss=signal.get("stop_loss"), rr_ratio=signal.get("rr_ratio"),
            status="PENDING",
            price_at_signal=signal.get("price"),
            reasons_json=json.dumps(signal.get("reasons", [])),
            indicators_json=json.dumps(ind),
            sent_to_telegram=telegram_msg_id is not None,
            telegram_msg_id=telegram_msg_id,
            ai_decision=ai_review.get("action") if ai_review else None,
            ai_adjusted_confidence=ai_review.get("adjusted_confidence") if ai_review else None,
            ai_reasoning=ai_review.get("reasoning") if ai_review else None,
            ml_win_prob=signal.get("ml_win_prob"),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info(f"Signal saved: {symbol} {signal['direction']} id={record.id}")
        return record


def is_duplicate(symbol: str, direction: str, style: str, window_minutes: int = 120) -> bool:
    """Cross-exchange dedup: same symbol+direction+style within window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    with SessionLocal() as db:
        count = db.query(func.count(SignalRecord.id)).filter(
            SignalRecord.symbol == symbol,
            SignalRecord.direction == direction,
            SignalRecord.style == style,
            SignalRecord.sent_to_telegram == True,
            SignalRecord.created_at >= cutoff,
        ).scalar()
        return (count or 0) > 0


def count_open_signals(symbol: Optional[str] = None) -> int:
    with SessionLocal() as db:
        q = db.query(func.count(SignalRecord.id)).filter(
            SignalRecord.outcome.is_(None),
            SignalRecord.sent_to_telegram == True,
        )
        if symbol:
            q = q.filter(SignalRecord.symbol == symbol)
        return q.scalar() or 0


def count_signals_last_hour() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    with SessionLocal() as db:
        return db.query(func.count(SignalRecord.id)).filter(
            SignalRecord.sent_to_telegram == True,
            SignalRecord.created_at >= cutoff,
        ).scalar() or 0


def get_stats(days: int = 7) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with SessionLocal() as db:
        rows = db.query(SignalRecord).filter(
            SignalRecord.created_at >= cutoff,
        ).all()
        db.expunge_all()

    closed = [r for r in rows if r.outcome in WIN_OUTCOMES + LOSS_OUTCOMES + ("BE",)]
    wins   = sum(1 for r in closed if r.outcome in WIN_OUTCOMES)
    losses = sum(1 for r in closed if r.outcome in LOSS_OUTCOMES)
    be     = sum(1 for r in closed if r.outcome == "BE")
    total_r = sum(r.r_multiple or 0 for r in closed)
    wr = wins / len(closed) * 100 if closed else 0

    return {
        "total": len(closed), "wins": wins, "losses": losses, "breakeven": be,
        "open": sum(1 for r in rows if r.outcome is None and r.sent_to_telegram),
        "nofill": sum(1 for r in rows if r.outcome == "NOFILL"),
        "win_rate": round(wr, 1),
        "total_r": round(total_r, 2),
        "expectancy_r": round(total_r / len(closed), 3) if closed else 0,
    }
